"""
===============
minimap2
===============

Runs `minimap2 <https://lh3.github.io/minimap2/>`_
aligner. 

This module is tested to work with the version of
``minimap2`` installed internally with `dms_tools2` (the
default when you initialize a :class:`Mapper` object
with `prog=None`). If you use that version of ``minimap2``,
you do not need to install ``minimap2`` separately.
"""


import os
import sys
import re
import io
import functools
import subprocess
import tempfile
import collections
import random

import Bio.SeqIO

from dms_tools2 import NTS

#: `options` argument to :class:`Mapper` that works well
#: for codon-mutant libraries such as those created for
#: deep mutational scanning. Indels are highly penalized
#: as they are not expected, and settings are used that
#: effectively call strings of consecutive nucleotide
#: mutations as expected during codon mutagenesis. The
#: queries are assumed to be in the same orientation as
#: the target.
OPTIONS_CODON_DMS = ['--for-only',
                     '-A2',
                     '-B4',
                     '-O12',
                     '-E2',
                     '--secondary=no',
                     '--end-bonus=8',
                    ]

#: `options` argument to :class:`Mapper` that works well
#: for libraries that contain viral genes expected to
#: potentially have both point mutations (at nucleotide, not
#: codon level) and some longer deletions.
#: The queries are assumed to be in the same orientation
#: as the target. These options resemble those suggested for
#: `PacBio IsoSeq <https://github.com/lh3/minimap2/blob/master/cookbook.md#map-iso-seq>`_
#: but use `-C0` and `-un` to avoid splice-site preference as
#: we want to trick the aligner into thinking long deletions are
#: spliced introns.
OPTIONS_VIRUS_W_DEL = [
                       '-x','splice',
                       '-un',
                       '-C0',
                       '--splice-flank=no',
                       '--mask-level=1',
                       '--secondary=no',
                       '--for-only',
                       '--end-seed-pen=2',
                       '--end-bonus=4',
                      ]

# namedtuple to hold alignments
Alignment = collections.namedtuple('Alignment',
        ['target', 'r_st', 'r_en', 'q_len', 'q_st',
         'q_en', 'strand', 'cigar_str', 'additional',
         'score'])
Alignment.__doc__ = "Alignment of a query to a target."
Alignment.target.__doc__ = "Name of target to which query was aligned."
Alignment.r_st.__doc__ = "Alignment start in target (0 based)."
Alignment.r_en.__doc__ = "Alignment end in target (0 based)."
Alignment.q_st.__doc__ = "Alignment start in query (0 based)."
Alignment.q_en.__doc__ = "Alignment end in query (0 based)."
Alignment.q_len.__doc__ = "Total length of query prior to any clipping."
Alignment.strand.__doc__ = "1 if aligns in forward polarity, -1 if in reverse."
Alignment.cigar_str.__doc__ = "CIGAR in `PAF long format <https://github.com/lh3/minimap2#cs>`_"
Alignment.additional.__doc__ = ("List of additional :class:`Alignment` "
        "objects, useful for multiple alignments.")
Alignment.score.__doc__ = 'Alignment score.'


def checkAlignment(a, target, query):
    """Checks alignment is valid given target and query.

    Arguments:
        `a` (:class:`Alignment`)
            The alignment.
        `target` (str)
            The target to which `query` is aligned.
        `query` (str)
            The query aligned to `target`.

    Returns:
        `True` if `a` is a valid alignment of `query`
        to `target`, `False` otherwise. Being valid does
        not mean an alignment is good, just that the 
        start / ends and CIGAR in `a` are valid.

    >>> target = 'ATGCAT'
    >>> query = 'TACA'
    >>> a_valid = Alignment(target='target', r_st=1, r_en=5,
    ...         q_st=0, q_en=4, q_len=4, strand=1,
    ...         cigar_str='=T*ga=CA', additional=[], score=-1)
    >>> checkAlignment(a_valid, target, query)
    True
    >>> a_invalid = a_valid._replace(r_st=0, r_en=4)
    >>> checkAlignment(a_invalid, target, query)
    False
    """
    assert a.strand == 1, "not implemented for - strand"
    (cigar_query, cigar_target) = cigarToQueryAndTarget(a.cigar_str)
    if (
            (a.q_len < a.q_en) or
            (a.r_st >= a.r_en) or
            (a.q_st >= a.q_en) or
            (a.q_len != len(query)) or
            (query[a.q_st : a.q_en] != cigar_query) or
            (target[a.r_st : a.r_en] != cigar_target)
            ):
        return False
    else:
        return True


class Mapper:
    """Class to run ``minimap2`` and get results.

    Args:
        `targetfile` (str)
            FASTA file with target (reference) to which we align
            reads.
        `options` (list)
            Command line options to ``minimap2``. For 
            recommended options, for different situations, see:

                - :data:`OPTIONS_CODON_DMS`

                - :data:`OPTIONS_VIRUS_W_DEL`

        `prog` (str or `None`)
            Path to ``minimap2`` executable. `None` uses the
            version of ``minimap2`` installed internally with
            `dms_tools2`. This is recommended unless you have 
            some other preferred version.
        `target_isoforms` (dict)
            Sometimes targets might be be isoforms of each
            other. You can specify that with this dict, which
            is keyed by target names and has values that are sets
            of other targets. If targets `M1` and `M2` are isoforms,
            set `target_isoforms={'M1':['M2'], 'M2':['M1']}`.
            This argument is just used to set the `target_isoforms`
            attribute, but isn't used during alignment.

    Attributes:
        `targetfile` (str)
            Target (reference) file set at initialization.
        `targetseqs` (dict)
            Sequences in `targetfile`. Keys are sequence
            names, values are sequences as strings.
        `prog` (str)
            Path to ``minimap2`` set at initialization.
        `options` (list)
            Options to ``minimap2`` set at initialization.
        `version` (str)
            Version of ``minimap2``.
        `target_isoforms` (dict)
            Isoforms for each target. This is the value set
            by `target_isoforms` at initialization plus
            ensuring that each target is listed as an isoform
            of itself.

    Here is an example where we align a few reads to two target
    sequences.

    First, we generate a few target sequences:

    >>> targetlen = 200
    >>> random.seed(1)
    >>> targets = {}
    >>> for i in [1, 2]:
    ...     targets['target{0}'.format(i)] = ''.join(random.choice(NTS)
    ...             for _ in range(targetlen))

    Now we generate some queries. One is a random sequence that should not
    align, and the other two are substrings of the targets into which we
    have introduced a single mutation or indel. The names of the queries
    give their target, start in query, end in query, cigar string:

    >>> queries = {'randseq':''.join(random.choice(NTS) for _ in range(180))}
    >>> for qstart, qend, mut in [(0, 183, 'mut53'), (36, 194, 'del140')]:
    ...     target = random.choice(list(targets.keys()))
    ...     qseq = targets[target][qstart : qend]
    ...     mutsite = int(mut[3 : ])
    ...     if 'mut' in mut:
    ...         wt = qseq[mutsite]
    ...         mut = random.choice([nt for nt in NTS if nt != wt])
    ...         cigar = ('=' + qseq[ : mutsite] + '*' + wt.lower() +
    ...                  mut.lower() + '=' + qseq[mutsite + 1 : ])
    ...         qseq = qseq[ : mutsite] + mut + qseq[mutsite + 1 : ]
    ...     elif 'del' in mut:
    ...         cigar = ('=' + qseq[ : mutsite] + '-' +
    ...                  qseq[mutsite].lower() + '=' + qseq[mutsite + 1 : ])
    ...         qseq = qseq[ : mutsite] + qseq[mutsite + 1 : ]
    ...     queryname = '_'.join(map(str, [target, qstart, qend, cigar]))
    ...     queries[queryname] = qseq


    Now map the queries to the targets:

    >>> TempFile = functools.partial(tempfile.NamedTemporaryFile, mode='w')
    >>> with TempFile() as targetfile, TempFile() as queryfile:
    ...     _ = targetfile.write('\\n'.join('>{0}\\n{1}'.format(*tup)
    ...                          for tup in targets.items()))
    ...     targetfile.flush()
    ...     _ = queryfile.write('\\n'.join('>{0}\\n{1}'.format(*tup)
    ...                         for tup in queries.items()))
    ...     queryfile.flush()
    ...     mapper = Mapper(targetfile.name, OPTIONS_CODON_DMS)
    ...     mapper2 = Mapper(targetfile.name, OPTIONS_CODON_DMS,
    ...             target_isoforms={'target1':{'target2'},
    ...                              'target2':{'target1'}})
    ...     alignments = mapper.map(queryfile.name)
    >>> mapper.targetseqs == targets
    True

    Now make sure we find the expected alignments:

    >>> set(alignments.keys()) == set(q for q in queries if q != 'randseq')
    True
    >>> matched = []
    >>> for (query, a) in alignments.items():
    ...     expected = query.split('_')
    ...     matched.append(a.target == expected[0])
    ...     matched.append([a.r_st, a.r_en] == list(map(int, expected[1 : 3])))
    ...     matched.append([a.q_st, a.q_en] == [0, len(queries[query])])
    ...     matched.append(a.cigar_str == expected[3])
    ...     matched.append(a.strand == 1)
    >>> all(matched)
    True

    Test out the `target_isoform` argument:

    >>> mapper.target_isoforms == {'target1':{'target1'}, 'target2':{'target2'}}
    True
    >>> mapper2.target_isoforms == {'target1':{'target1', 'target2'},
    ...         'target2':{'target1', 'target2'}}
    True
    """

    def __init__(self, targetfile, options, *, prog=None,
            target_isoforms={}):
        """See main :class:`Mapper` doc string."""
        if prog is None:
            # use default ``minimap2`` installed as package data
            prog = os.path.join(os.path.dirname(__file__),
                                'minimap2_prog')

        try:
            version = subprocess.check_output([prog, '--version'])
        except:
            raise ValueError("Can't execute `prog` {0}".format(prog))
        self.version = version.strip().decode('utf-8')
        self.prog = prog
        self.options = options
        assert os.path.isfile(targetfile), \
                "no `targetfile` {0}".format(targetfile)
        self.targetfile = targetfile
        self.targetseqs = {seq.name:str(seq.seq) for seq in 
                      Bio.SeqIO.parse(self.targetfile, 'fasta')}

        targetnames = set(self.targetseqs.keys())
        in_target_isoforms = set(target_isoforms.keys()).union(
                set(t for tl in target_isoforms.values() for t in tl))
        if in_target_isoforms - targetnames:
            raise ValueError("`target_isoforms` contains following "
                             "targets not in `targetfile`: {0}".format(
                             in_target_isoforms - targetnames))
        self.target_isoforms = {}
        for target in targetnames:
            self.target_isoforms[target] = {target}
            if target in target_isoforms:
                addtl_targets = target_isoforms[target]
                if isinstance(addtl_targets, list):
                    addtl_targets = set(addtl_targets)
                self.target_isoforms[target].update(addtl_targets)


    def map(self, queryfile, *, outfile=None, introns_to_gaps=True,
            shift_indels=True, check_alignments=True):
        """Map query sequences to target.

        Aligns query sequences to targets. Adds ``--c --cs=long``
        arguments to `options` to get a long CIGAR string, and
        returns results as a dictionary, and optionally writes them
        to a PAF file.

        This is **not** a memory-efficient implementation as
        a lot is read into memory. So if you have very large
        queries or targets, that may pose a problem.

        Args:
            `queryfile` (str)
                FASTA file with query sequences to align.
                Headers should be unique.
            `outfile` (`None` or str)
                Name of output file containing alignment
                results in PAF format if a str is provided.
                Provide `None` if you don't want to create
                a permanent alignment file.
            `introns_to_gaps` (bool)
                If there are introns in the alignment CIGARs, convert
                to gaps by running through :meth:`intronsToGaps`.
            `shift_indels` (bool)
                Pass alignments through :meth:`shiftIndels`.
                Only applies to alignments returned in dict,
                does not affect any results in `outfile`.
            `check_alignments` (bool)
                Run all alignments through :meth:`checkAlignment`
                before returning, and raise error if any are invalid.
                This is a good debugging check, but costs time.

        Returns:
            A dict where keys are the name of each query in
            `queryfile` for which there is an alignment (there
            are no keys for queries that do not align). If there
            is a single alignment for a query, the value is that
            :class:`Alignment` object. There can be multiple primary
            alignments (`see here <https://github.com/lh3/minimap2/issues/113>`_).
            If there are multiple alignments, the value is the 
            :class:`Alignment` with the highest score, and the remaining
            alignments are listed in the :class:`Alignment.additional`
            attribute of that "best" alignment.
        """
        assert os.path.isfile(queryfile), "no `queryfile` {0}".format(queryfile)

        assert '-a' not in self.options, \
                "output should be PAF format, not SAM"
        for arg in ['-c', '--cs=long']:
            if arg not in self.options:
                self.options.append(arg)

        if outfile is None:
            fout = tempfile.TemporaryFile('w+')
        else:
            fout = open(outfile, 'w+')
        stderr = tempfile.TemporaryFile()
        try:
            _ = subprocess.check_call(
                    [self.prog] + self.options + [self.targetfile, queryfile],
                    stdout=fout, stderr=stderr)
            fout.seek(0)
            dlist = collections.defaultdict(list)
            for query, alignment in parsePAF(fout,
                    self.targetseqs, introns_to_gaps):
                dlist[query].append(alignment)
        except:
            stderr.seek(0)
            sys.stderr.write('\n{0}\n'.format(stderr.read()))
            raise
        finally:
            fout.close()
            stderr.close()

        d = {}
        for query in list(dlist.keys()):
            if len(dlist[query]) == 1:
                d[query] = dlist[query][0]
            else:
                assert len(dlist[query]) > 1
                sorted_alignments = [tup[1] for tup in sorted(
                        [(a.score, a) for a in dlist[query]],
                        reverse=True)]
                d[query] = sorted_alignments[0]._replace(
                        additional=sorted_alignments[1 : ])
            del dlist[query]

        if shift_indels:
            for query in list(d.keys()):
                new_cigar_str = shiftIndels(d[query].cigar_str)
                if new_cigar_str != d[query].cigar_str:
                    d[query] = d[query]._replace(cigar_str=new_cigar_str)

        if check_alignments:
            queryseqs = {seq.name:str(seq.seq) for seq in
                         Bio.SeqIO.parse(queryfile, 'fasta')}
            for query, a in d.items():
                if not checkAlignment(a, self.targetseqs[a.target],
                        queryseqs[query]):
                    raise ValueError("Invalid alignment for {0}.\n"
                            "alignment = {1}\ntarget = {2}\nquery = {3}"
                            .format(query, a, self.targetseqs[a.target],
                            queryseqs[query]))

        return d


#: match indels for :meth:`shiftIndels`
_INDELMATCH = re.compile('(?P<lead>=[A-Z]+)'
                         '(?P<indeltype>[\-\+])'
                         '(?P<indel>[a-z]+)'
                         '(?P<trail>=[A-Z]+)')

def shiftIndels(cigar):
    """Shifts indels to consistent position.

    In some cases it is ambiguous where to place insertions /
    deletions in the CIGAR string. This function moves them
    to a consistent location (as far forward as possible).

    Args:
        `cigar` (str)
            PAF long CIGAR string, format is 
            `detailed here <https://github.com/lh3/minimap2#cs>`_.

    Returns:
        A version of `cigar` with indels shifted as far
        forward as possible.

    >>> shiftIndels('=AAC-atagcc=GGG-ac=T')
    '=AA-catagc=CGGG-ac=T'

    >>> shiftIndels('=AAC-atagac=GGG-acg=AT')
    '=A-acatag=ACGG-gac=GAT'

    >>> shiftIndels('=TCC+c=TCAGA+aga=CT')
    '=T+c=CCTC+aga=AGACT'
    """
    i = 0
    m = _INDELMATCH.search(cigar[i : ])
    while m:
        n = 0
        indel = m.group('indel').upper()
        while m.group('lead')[-n - 1 : ] == indel[-n - 1 : ]:
            n += 1
        if n > 0:
            if n == len(m.group('lead')) - 1:
                lead = '' # removed entire lead
            else:
                lead = m.group('lead')[ : -n]
            shiftseq = m.group('lead')[-n : ] # sequence to shift
            cigar = ''.join([
                    cigar[ : i + m.start('lead')], # sequence before match
                    lead, # remaining portion of lead
                    m.group('indeltype'),
                    shiftseq.lower(), m.group('indel')[ : -n], # new indel
                    '=', shiftseq, m.group('trail')[1 : ], # trail after indel
                    cigar[i + m.end('trail') : ] # sequence after match
                    ])
        else:
            i += m.start('trail')
        m = _INDELMATCH.search(cigar[i : ])

    return cigar


def trimCigar(side, cigar):
    """Trims a nucleotide from CIGAR string.

    Currently just trims one site.

    Args:
        `side` (str)
            "start" trim from start, "end" to trim from end.
        `cigar` (str)
            PAF long CIGAR string, format is 
            `detailed here <https://github.com/lh3/minimap2#cs>`_.

    Returns:
        A version of `cigar` with a single site trimmed
        from start or end.

    >>> trimCigar('start', '=ATG')
    '=TG'
    >>> trimCigar('end', '=ATG')
    '=AT'
    >>> trimCigar('start', '*ac=TG')
    '=TG'
    >>> trimCigar('end', '=AT*ag')
    '=AT'
    >>> trimCigar('start', '-aac=TG')
    '-ac=TG'
    >>> trimCigar('end', '=TG+aac')
    '=TG+aa'
    """
    if side == 'start':
        if re.match('=[A-Z]{2}', cigar):
            return '=' + cigar[2 : ]
        elif re.match('=[A-Z][\*\-\+]', cigar):
            return cigar[2 : ]
        elif re.match('\*[a-z]{2}', cigar):
            return cigar[3 : ]
        elif re.match('[\-\+][a-z]{2}', cigar):
            return cigar[0] + cigar[2 : ]
        elif re.match('[\-\+][a-z][^a-z]'):
            return cigar[2 : ]
        else:
            raise ValueError("Cannot match start of {0}".format(cigar))
    elif side == 'end':
        if re.search('[A-Z]{2}$', cigar):
            return cigar[ : -1]
        elif re.search('=[A-Z]$', cigar):
            return cigar[ : -2]
        elif re.search('\*[a-z]{2}$', cigar):
            return cigar[ : -3]
        elif re.search('[\-\+][a-z]$', cigar):
            return cigar[ : -2]
        elif re.search('[a-z]{2}$', cigar):
            return cigar[ : -1]
        else:
            raise ValueError("Cannot match end of {0}".format(cigar))
    else:
        raise ValueError("`side` must be 'start' or 'end', got {0}"
                         .format(side))



def parsePAF(paf_file, targets=None, introns_to_gaps=False):
    """Parse ``*.paf`` file as created by ``minimap2``.

    `paf_file` is assumed to be created with
    the ``minimap2`` options ``-c --cs=long``, which
    creates long `cs` tags with the CIGAR string.

    PAF format is `described here <https://github.com/lh3/miniasm/blob/master/PAF.md>`_.
    PAF long CIGAR string format from ``minimap2`` is 
    `detailed here <https://github.com/lh3/minimap2#cs>`_.

    Args:
        `paf_file` (str or iterator)
            If str, should be name of ``*.paf`` file.
            Otherwise should be an iterator that returns
            lines as would be read from a PAF file.
        `targets` (dict or `None`)
            If not `None`, is dict keyed by target names,
            with values target sequences. You need to provide
            this if ``minimap2`` is run with a ``--splice``
            option and `introns_to_gaps` is `True`.
        `introns_to_gap` (bool)
            Pass CIGAR strings through :meth:`intronsToGaps`.
            Requires you to provide `targets`.

    Returns:
        A generator that yields query / alignments on each line in 
        `paf_file`. Returned as the 2-tuple `(query_name, a)`,
        where `query_name` is a str giving the name of the query
        sequence, and `a` is an :class:`Alignment`.

    Here is a short example:

    >>> paf_file = io.StringIO('\\t'.join([
    ...         'myquery', '10', '0', '10', '+', 'mytarget',
    ...         '20', '5', '15', '9', '10', '60',
    ...         'cs:Z:=ATG*ga=GAACAT', 'AS:i:7']))
    >>> alignments = [tup for tup in parsePAF(paf_file)]
    >>> len(alignments)
    1
    >>> (queryname, alignment) = alignments[0]
    >>> queryname
    'myquery'
    >>> alignment.target
    'mytarget'
    >>> (alignment.r_st, alignment.r_en)
    (5, 15)
    >>> (alignment.q_st, alignment.q_en)
    (0, 10)
    >>> alignment.strand
    1
    >>> alignment.cigar_str
    '=ATG*ga=GAACAT'
    >>> alignment.q_len
    10
    >>> alignment.score
    7

    Now an example of using `targets` and `introns_to_gaps`.
    You can see that this option converts the ``~gg5ac``
    to ``-ggaac`` in the `cigar_str` attribute:

    >>> targets = {'mytarget':'ATGGGAACAT'}
    >>> paf_file = io.StringIO('\\t'.join([
    ...         'myquery', '9', '0', '9', '+', 'mytarget',
    ...         '10', '1', '10', '?', '4', '60',
    ...         'cs:Z:=TG~gg5ac=AT', 'AS:i:2']))
    >>> a_keep_introns = [tup for tup in parsePAF(paf_file)][0][1]
    >>> _ = paf_file.seek(0)
    >>> a_introns_to_gaps = [tup for tup in parsePAF(paf_file,
    ...         targets=targets, introns_to_gaps=True)][0][1]
    >>> a_keep_introns.cigar_str
    '=TG~gg5ac=AT'
    >>> a_introns_to_gaps.cigar_str
    '=TG-ggaac=AT'
    """
    if introns_to_gaps and (not targets or not isinstance(targets, dict)):
        raise ValueError("specify `target` if `introns_to_gaps`:\n{0}"
                         .format(targets))

    cigar_m = re.compile(
            'cs:Z:(?P<cigar_str>('
            '\*[a-z]{2}|' # matches mutations
            '=[A-Z]+|' # matches identities
            '[\+\-][a-z]+|' # matches indels
            '\~[a-z]{2}\d+[a-z]{2}' # matches introns
            ')+)(?:\s+|$)')
    score_m = re.compile('AS:i:(?P<score>\d+)(?:\s+|$)')

    close_paf_file = False
    if isinstance(paf_file, str):
        assert os.path.isfile(paf_file), "no `paf_file` {0}".format(
                paf_file)
        paf_file = open(paf_file, 'r')
        close_paf_file = True

    elif not isinstance(paf_file, collections.Iterable):
        raise ValueError("`paf_file` must be file name or iterable")

    for line in paf_file:
        entries = line.split('\t', maxsplit=12)
        try:
            cigar_str = cigar_m.search(entries[12]).group('cigar_str')
        except:
            raise ValueError("Cannot match CIGAR:\n{0}".format(entries[12]))
        try:
            score = int(score_m.search(entries[12]).group('score'))
        except:
            raise ValueError("Cannot match score:\n{0}".format(entries[12]))
        query_name = entries[0]
        target = entries[5]
        r_st = int(entries[7])
        r_en = int(entries[8])
        if introns_to_gaps:
            try:
                targetseq = targets[target]
            except KeyError:
                raise KeyError("No target {0} in targets".format(target))
            cigar_str = intronsToGaps(cigar_str, targetseq[r_st : r_en])
        a = Alignment(target=target,
                      r_st=r_st,
                      r_en=r_en,
                      q_st=int(entries[2]),
                      q_en=int(entries[3]),
                      q_len=int(entries[1]),
                      strand={'+':1, '-':-1}[entries[4]],
                      cigar_str=cigar_str, 
                      additional=[],
                      score=score)
        yield (query_name, a)

    if close_paf_file:
        paf_file.close()


#: matches an exact match group in long format CIGAR
_EXACT_MATCH = re.compile('=[A-Z]+')

def numExactMatches(cigar):
    """Number exactly matched nucleotides in long CIGAR.

    >>> numExactMatches('=ATG-aca=A*gc+ac=TAC')
    7
    """
    n = 0
    for m in _EXACT_MATCH.finditer(cigar):
        n += len(m.group()) - 1
    return n


#: matches individual group in long format CIGAR
_CIGAR_GROUP_MATCH = re.compile('=[A-Z]+|' # exact matches
                                '\*[a-z]{2}|' # mutation
                                '[\-\+[a-z]+|' # indel
                                '\~[a-z]{2}\d+[a-z]{2}' # intron
                                )


def intronsToGaps(cigar, target):
    """Converts introns to gaps in CIGAR string.

    If you run ``minimap2``, it reports introns differently
    than gaps in the target. This function converts
    the intron notation to gaps. This is useful if you are
    using the introns as an ad-hoc way to identify
    long gaps.

    Args:
        `cigar` (str)
            PAF long CIGAR string, format is 
            `detailed here <https://github.com/lh3/minimap2#cs>`_.
        `target` (str)
            The exact portion of the target aligned to the query
            in `cigar`.

    >>> target = 'ATGGAACTAGCATCTAG'
    >>> cigar = '=A+ca=TG-g=A*ag=CT~ag5at=CTAG'
    >>> intronsToGaps(cigar, target)
    '=A+ca=TG-g=A*ag=CT-agcat=CTAG'
    """
    newcigar = []
    i = 0 # index in target
    while cigar:
        m = _CIGAR_GROUP_MATCH.match(cigar)
        assert m, "can't match CIGAR:\n{0}".format(cigar)
        assert m.start() == 0
        if m.group()[0] == '=':
            newcigar.append(m.group())
            i += m.end() - 1
        elif m.group()[0] == '*':
            newcigar.append(m.group())
            i += 1
        elif m.group()[0] == '-':
            newcigar.append(m.group())
            i += m.end() - 1
        elif m.group()[0] == '+':
            newcigar.append(m.group())
        elif m.group()[0] == '~':
            intronlen = int(m.group()[3 : -2])
            newcigar += ['-', target[i : i + intronlen].lower()]
            assert m.group()[1 : 3].upper() == target[i : i + 2], \
                    "target = {0}\ncigar = {1}".format(target, cigar)
            i += intronlen
            assert m.group()[-2 : ].upper() == target[i - 2 : i]
        else:
            raise RuntimeError('should never get here')
        cigar = cigar[m.end() : ]

    return ''.join(newcigar)


def cigarToQueryAndTarget(cigar):
    """Returns `(query, target)` specified by PAF long CIGAR.

    Cannot handle CIGAR strings with intron operations.
    To check those, you first need to run through
    :meth:`intronsToGaps`.

    Args:
        `cigar` (str)
            CIGAR string.

    Returns:
        The 2-tuple `(query, target)`, where each is
        a string giving the encoded query and target.
    
    >>> cigarToQueryAndTarget('=AT*ac=G+at=AG-ac=T')
    ('ATCGATAGT', 'ATAGAGACT')
    """
    assert isinstance(cigar, str)
    query = []
    target = []
    while cigar:
        m = _CIGAR_GROUP_MATCH.match(cigar)
        assert m, "can't match CIGAR:\n{0}".format(cigar)
        assert m.start() == 0
        if m.group()[0] == '=':
            query.append(m.group()[1 : ])
            target.append(m.group()[1 : ])
        elif m.group()[0] == '*':
            query.append(m.group()[2].upper())
            target.append(m.group()[1].upper())
        elif m.group()[0] == '-':
            target.append(m.group()[1 : ].upper())
        elif m.group()[0] == '+':
            query.append(m.group()[1 : ].upper())
        elif m.group()[0] == '~':
            raise ValueError("Cannot handle intron operations, but."
                    "string has one:\n{0}".format(m.group()))
        else:
            raise RuntimeError('should never get here')
        cigar = cigar[m.end() : ]
    return (''.join(query), ''.join(target))


def mutateSeq(wtseq, mutations, insertions, deletions):
    """Mutates sequence and gets CIGAR.

    Primarily useful for simulations.

    In the mutation specifications below, 0-based numbering
    is used. Sequence characters are upper case nucleotides.
    Operations are applied in the order: mutations, insertions,
    deletions. So a deletion can overwrite a mutation or insertion.
    The entire insertion counts as just one added site when indexing
    the deletions. You will get an error if deletions and insertions
    overlap.

    Arguments:
        `wtseq` (str)
            The wildtype sequence.
        `mutations` (list)
            List of point mutations in form `(i, mut)` where
            `i` is site and `mut` is mutant amino acid (can be
            same as wildtype).
        `deletions` (list)
            List of deletion locations in form `(istart, iend)`.
        `insertions` (list)
            List of insertions in form `(i, seqtoinsert)`.

    Returns:
        The 2-tuple `(mutantseq, cigar)` where `cigar` is the CIGAR
        in `PAF long format <https://github.com/lh3/minimap2#cs>`_.

    Here is an example:

    >>> wtseq = 'ATGGAATGA'
    >>> (mutantseq, cigar) = mutateSeq(wtseq, [], [], [])
    >>> mutantseq == wtseq
    True
    >>> cigar == '=' + wtseq
    True

    >>> (mutantseq, cigar) = mutateSeq(wtseq,
    ...         [(0, 'C'), (1, 'T'), (3, 'A')],
    ...         [(8, 'TAC')], [(5, 2)])
    >>> mutantseq
    'CTGAAGTACA'
    >>> cigar
    '*ac=TG*ga=A-at=G+tac=A'
    """
    assert re.match('^[{0}]+$'.format(''.join(NTS)), wtseq), \
            "`wtseq` not all upper case nucleotides."
    n = len(wtseq)
    mutantseq = list(wtseq)
    cigar = mutantseq.copy()

    for i, mut in mutations:
        assert 0 <= i < n
        if mut.upper() != wtseq[i]:
            mutantseq[i] = mut.upper()
            cigar[i] = '*' + wtseq[i].lower() + mut.lower()

    # traverse indels from back so index is maintained
    for i, seqtoinsert in sorted(insertions, reverse=True):
        mutantseq.insert(i, seqtoinsert.upper())
        cigar.insert(i, '+' + seqtoinsert.lower())

    for i, del_len in sorted(deletions, reverse=True):
        delseq = []
        for j in range(i, i + del_len):
            if mutantseq[j] in NTS:
                delseq.append(mutantseq[j])
            elif mutantseq[j][0] == '*':
                delseq.append(mutantseq[j][1])
            else:
                raise ValueError("overlapping insertions and deletions")
        for _ in range(del_len):
            mutantseq.pop(i)
            cigar.pop(i)
        cigar.insert(i, '-' + ''.join(delseq).lower())

    # add equal signs to cigar
    for i, op in list(enumerate(cigar)):
        if (op in NTS) and (i == 0 or cigar[i - 1][-1] not in NTS):
            cigar[i] = '=' + op

    mutantseq = ''.join(mutantseq)
    cigar = ''.join(cigar)
    assert mutantseq == cigarToQueryAndTarget(cigar)[0]

    return (mutantseq, cigar)



if __name__ == '__main__':
    import doctest
    doctest.testmod()
