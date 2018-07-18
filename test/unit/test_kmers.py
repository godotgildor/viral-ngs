"""Unit tests for kmers.py"""

__author__ = "ilya@broadinstitute.org"

import os
import sys
import collections
import argparse
import inspect
import logging
import itertools
import traceback

from test import assert_equal_contents, assert_equal_bam_reads, make_slow_test_marker

import Bio.SeqIO
from Bio.Seq import Seq
from Bio.SeqRecord import SeqRecord
from Bio.Alphabet import IUPAC
import pytest

import kmers
import read_utils
import util.cmd
import util.file
import util.misc
import tools.kmc
import tools.samtools

log = logging.getLogger(__name__)  # pylint: disable=invalid-name

slow_test = make_slow_test_marker()  # pylint: disable=invalid-name

#################################
# Some general utils used below #
#################################

def _seq_as_str(s):  # pylint: disable=invalid-name
    """Return a sequence as a str, regardless of whether it was a str, a Seq or a SeqRecord"""
    if isinstance(s, Seq):
        return str(s)
    if isinstance(s, SeqRecord):
        return str(s.seq)
    return s

def _yield_seq_recs(seq_file):
    """Yield sequence records from the file, regardless of file format."""
    with util.file.tmp_dir(suffix='_seqs_as_strs') as t_dir:
        if seq_file.endswith('.bam'):
            t_fa = os.path.join(t_dir, 'bam2fa.fasta')
            tools.samtools.SamtoolsTool().bam2fa(seq_file, t_fa, append_mate_num=True)
            seq_file = t_fa
        with util.file.open_or_gzopen(seq_file, 'rt') as seq_f:
            for rec in Bio.SeqIO.parse(seq_f, util.file.uncompressed_file_type(seq_file)[1:]):
                yield rec

def _list_seq_recs(seq_file):
    """Return a list of sequence records from the file, regardless of file format."""
    return list(_yield_seq_recs(seq_file))


def _yield_seqs_as_strs(seqs):
    """Yield sequence(s) from `seqs` as strs.  seqs can be a str/SeqRecord/Seq, a filename of a sequence file,
    or an iterable of these.  If a filename of a sequence file, all sequences from that file are yielded."""
    for seq in util.misc.make_seq(seqs, (str, SeqRecord, Seq)):
        seq = _seq_as_str(seq)
        if not any(seq.endswith(ext) for ext in '.fasta .fasta.gz .fastq .fastq.gz .bam'):
            yield seq
        else:
            for rec in _yield_seq_recs(seq):
                yield str(rec.seq)

def _list_seqs_as_strs(seqs):
    """Return a list of sequence(s) from `seqs` as strs.  seqs can be a str/SeqRecord/Seq, a filename of a sequence file,
    or an iterable of these.  If a filename of a sequence file, all sequences from that file are yielded."""
    return list(_yield_seqs_as_strs(seqs))

def _getargs(args, valid_args):
    """Extract valid args from an argparse.Namespace or a dict.  Returns a dict containing keys from `args`
    that are in `valid_args`; `valid_args` is a space-separated list of valid args."""
    return util.misc.subdict(vars(args) if isinstance(args, argparse.Namespace) else args, valid_args.split())

def _strip_mate_num(rec_id):
    """Name of a read, with any /1 or /2 at the end removed"""
    return rec_id[:-2] if rec_id.endswith('/1') or rec_id.endswith('/2') else rec_id

#################################################################################################################


class KmcPy(object):
    """Reimplementation of some kmc functions in simple Python code.

    To help generate the expected correct output, we reimplement in simple Python code some
    of KMC's functionality.   This is also to make up for KMC's lack of a public test suite
    ( https://github.com/refresh-bio/KMC/issues/55 ).
    """

    def _revcomp(self, kmer):
        """Return the reverse complement of a kmer, given as a string"""
        assert isinstance(kmer, str)
        return str(Seq(kmer, IUPAC.unambiguous_dna).reverse_complement())

    def _canonicalize(self, kmer):
        """Return the canonical version of a kmer"""
        return min(kmer, self._revcomp(kmer))

    def _compute_kmers_iter(self, seq_strs, kmer_size, single_strand, **ignore):
        """Yield kmers of seq(s).  Unless `single_strand` is True, each kmer
        is canonicalized before being returned.  Note that
        deduplication is not done, so each occurrence of each kmer is
        yielded.  Kmers containing non-TCGA bases are skipped.
        """
        for seq in seq_strs:
            n_kmers = len(seq)-kmer_size+1

            # mark kmers containing invalid base(s)
            valid_kmer = [True] * n_kmers
            for i in range(len(seq)):  # pylint: disable=consider-using-enumerate
                if seq[i].upper() not in 'TCGA':
                    invalid_lo = max(0, i-kmer_size+1)
                    invalid_hi = min(n_kmers, i+1)
                    valid_kmer[invalid_lo:invalid_hi] = [False]*(invalid_hi-invalid_lo)

            for i in range(n_kmers):
                if valid_kmer[i]:
                    kmer = seq[i:i+kmer_size]
                    yield kmer if single_strand else self._canonicalize(kmer)

    def _compute_kmers(self, *args, **kw):
        """Return list of kmers of seq(s).  Unless `single_strand` is True, each kmer
        is canonicalized before being returned.  Note that
        deduplication is not done, so each occurrence of each kmer is
        yielded.  Kmers containing non-TCGA bases are skipped.
        """
        return list(self._compute_kmers_iter(*args, **kw))

    def compute_kmer_counts(self, seq_files, kmer_size, min_occs=None, max_occs=None,
                            counter_cap=None, single_strand=False, **ignore):
        """Yield kmer counts of seq(s).  Unless `single_strand` is True, each kmer is
        canonicalized before being counted.  Kmers containing non-TCGA bases are skipped.
        Kmers with fewer than `min_occs` or more than `max_occs` occurrences
        are dropped, and kmer counts capped at `counter_cap`, if these args are given.
        """
        counts = collections.Counter(self._compute_kmers(_list_seqs_as_strs(seq_files), kmer_size, single_strand))
        if any((min_occs, max_occs, counter_cap)):
            counts = dict((kmer, min(count, counter_cap or count)) \
                          for kmer, count in counts.items() \
                          if (count >= (min_occs or count)) and \
                          (count <= (max_occs or count)))
        return counts

    def filter_kmers(self, db_kmer_counts, db_min_occs, db_max_occs):
        log.debug('filtering %d kmers', len(db_kmer_counts))
        result = {kmer for kmer, count in db_kmer_counts.items() if db_min_occs <= count <= db_max_occs}
        log.debug('done filtering %d kmers, got %d', len(db_kmer_counts), len(result))
        return result

    def filter_seqs(self, db_kmer_counts, in_reads, kmer_size, single_strand,
                    db_min_occs, db_max_occs, read_min_occs, read_max_occs,
                    read_min_occs_frac, read_max_occs_frac, **ignore):
        log.debug('kmercounts=%s', sorted(collections.Counter(db_kmer_counts.values()).items()))
        db_kmers= self.filter_kmers(db_kmer_counts=db_kmer_counts, db_min_occs=db_min_occs, db_max_occs=db_max_occs)

        seqs_ids_out = set()
        rel_thresholds = (read_min_occs_frac, read_max_occs_frac) != (0., 1.)
        in_recs = _list_seq_recs(in_reads)

        seq_occs_hist = collections.Counter()
        mate_cnt = collections.Counter()

        for rec in in_recs:
            seq = str(rec.seq)
            seq_kmer_counts = self.compute_kmer_counts(seq, kmer_size=kmer_size, single_strand=single_strand)
            assert not single_strand
            seq_occs = sum([seq_count for kmer, seq_count in seq_kmer_counts.items() \
                            if kmer in db_kmers])
            seq_occs_hist[seq_occs] += 1

            if rel_thresholds:
                n_seq_kmers = len(seq)-kmer_size+1
                read_min_occs_seq, read_max_occs_seq = (int(read_min_occs_frac * n_seq_kmers),
                                                        int(read_max_occs_frac * n_seq_kmers))
            else:
                read_min_occs_seq, read_max_occs_seq = (read_min_occs, read_max_occs)

            if read_min_occs_seq <= seq_occs <= read_max_occs_seq:
                seqs_ids_out.add(rec.id)
                mate_cnt[rec.id[-2:] if rec.id[-2:] in ('/1', '/2') else '/0'] += 1


        log.debug('kmer occs histogram: %s', sorted(seq_occs_hist.items()))
        log.debug('filter_seqs: %d of %d passed; mate_cnt=%s', len(seqs_ids_out), len(in_recs),
                  mate_cnt)

        return seqs_ids_out

# end: class KmcPy    

kmcpy = KmcPy()

def _inp(fname):
    """Return full path to a test input file for this module"""
    return os.path.join(util.file.get_test_input_path(), 'TestKmers', fname)

def _stringify(par): 
    return util.file.string_to_file_name(str(par))

BUILD_KMER_DB_TESTS = [
    ('empty.fasta', ''),
    ('ebola.fasta.gz', '-k 7'),
    ('almost-empty-2.bam', '-k 23 --singleStrand'),
    ('almost-empty-2.bam', '-k 5 --minOccs 1 --maxOccs 5 --counterCap 3'),
    ('test-reads.bam test-reads-human.bam', '-k 17'),
    ('tcgaattt.fasta', '-k 7')
]

@pytest.fixture(scope='module', ids=_stringify)
def kmer_db_fixture(request, tmpdir_module):
    """Build a database of kmers from given sequence file(s) using given options.

    Fixture param: a 2-tuple (seq_files, kmer_db_opts)
    Fixture return:
       an argparse.Namespace() with the following attrs:
          kmer_db: path to the kmer db created from seq_files
          kmc_kmer_counts: map from kmer to count, as computed by kmc
          kmc_db_args: the result of parsing kmer_db_opts
    """
    k_db = os.path.join(tmpdir_module, 'bld_kmer_db')
    seq_files, kmer_db_opts = request.param
    seq_files = list(map(_inp, seq_files.split()))
    kmer_db_args = util.cmd.run_cmd(module=kmers, cmd='build_kmer_db',
                                    args=seq_files + [k_db] + kmer_db_opts.split() + ['--memLimitGb', 4]).args_parsed
    kmc_kmer_counts=tools.kmc.KmcTool().get_kmer_counts(k_db, threads=kmer_db_args.threads)
    log.debug('KMER_DB_FIXTURE: param=%s counts=%d db=%s', request.param, len(kmc_kmer_counts), k_db)
    yield argparse.Namespace(kmer_db=k_db,
                             kmc_kmer_counts=kmc_kmer_counts,
                             kmer_db_args=kmer_db_args)


@pytest.mark.parametrize("kmer_db_fixture", BUILD_KMER_DB_TESTS, ids=_stringify, indirect=["kmer_db_fixture"])
def test_build_kmer_db(kmer_db_fixture):
    _test_build_kmer_db(kmer_db_fixture)

def _test_build_kmer_db(kmer_db_fixture):
    assert tools.kmc.KmcTool().is_kmer_db(kmer_db_fixture.kmer_db)

    kmer_db_info = tools.kmc.KmcTool().get_kmer_db_info(kmer_db_fixture.kmer_db)
    assert kmer_db_info.kmer_size == kmer_db_fixture.kmer_db_args.kmer_size
    assert kmer_db_info.min_occs == kmer_db_fixture.kmer_db_args.min_occs
    assert kmer_db_info.max_occs == kmer_db_fixture.kmer_db_args.max_occs

    kmcpy_kmer_counts = kmcpy.compute_kmer_counts(**vars(kmer_db_fixture.kmer_db_args))
    assert kmer_db_info.total_kmers == len(kmcpy_kmer_counts)
    assert kmer_db_fixture.kmc_kmer_counts == kmcpy_kmer_counts

###########
SEQ_FILES = [ 
    # 'empty.fasta',
    # 'ebola.fasta.gz',
    # 'almost-empty-2.bam',
    # 'test-reads.bam',
    # 'test-reads-human.bam',
    # 'tcgaattt.fasta',
    'G5012.3.fasta',
    'G5012.3.mini.bam',
]

KMER_SIZES = [1, 2, 7, 17, 27, 31, 55, 63]
#KMER_SIZES = [1, 17, 31, 55]
STRAND_OPTS = ['', '--singleStrand']
KMER_OCCS_OPTS = [ '', '--minOccs 1', '--minOccs 10', '--maxOccs 10' ]
NTHREADS = [1, 8]
COMBO_OPTS = [(seq_file, '-k{} {} {} --threads {}'.format(kmer_size, strand_opt, kmer_occs_opt, nthreads))
              for seq_file, kmer_size, strand_opt, kmer_occs_opt, nthreads \
              in itertools.product(SEQ_FILES, KMER_SIZES, STRAND_OPTS,
                                   KMER_OCCS_OPTS, NTHREADS)]

@slow_test
@pytest.mark.parametrize("kmer_db_fixture", COMBO_OPTS, ids=_stringify, indirect=["kmer_db_fixture"])
def test_build_kmer_db_combo(kmer_db_fixture):
    _test_build_kmer_db(kmer_db_fixture)


##############################################################################################

def _test_filter_by_kmers(kmer_db_fixture, reads_file, filter_opts, tmpdir_function):
    """Test read filtering.

    Args:
      kmer_size: kmer size
      single_strand: whether to canonicalize kmers
      kmers_fasta: kmers for filtering will be extracted from here
      reads_bam: reads to filter with kmers extracted from kmers_fasta

    """
    assert tools.kmc.KmcTool().is_kmer_db(kmer_db_fixture.kmer_db)

    reads_file = _inp(reads_file)
    reads_file_out = os.path.join(tmpdir_function, 'reads_out' + util.file.uncompressed_file_type(reads_file))

    filter_args = util.cmd.run_cmd(module=kmers, cmd='filter_by_kmers',
                                   args=[kmer_db_fixture.kmer_db, 
                                         reads_file, reads_file_out] + filter_opts.split()).args_parsed

    log.debug('Running filte: kmer_db_args=%s filter_arg=%s', kmer_db_fixture.kmer_db_args, filter_args)
    filtered_seqs_ids_expected = kmcpy.filter_seqs(db_kmer_counts=kmer_db_fixture.kmc_kmer_counts,
                                                   kmer_size=kmer_db_fixture.kmer_db_args.kmer_size,
                                                   single_strand=kmer_db_fixture.kmer_db_args.single_strand,
                                                   **vars(filter_args))

    reads_file_out_ids_txt = reads_file_out+'.ids.txt'
    read_utils.read_names(reads_file_out, reads_file_out_ids_txt)
    reads_out_ids = util.file.slurp_file(reads_file_out_ids_txt).strip().split()

    log.debug('FILT %d %d %s %s %s', len(_list_seq_recs(reads_file)), len(_list_seq_recs(reads_file_out)),
              kmer_db_fixture.kmer_db, reads_file, filter_opts)
    def normed_read_ids(ids): return set(map(_strip_mate_num, ids))

    assert normed_read_ids(reads_out_ids) == normed_read_ids(filtered_seqs_ids_expected)

# end: def _test_filter_by_kmers(kmer_db_fixture, reads_file, filter_opts, tmpdir_function)

@pytest.mark.parametrize("kmer_db_fixture", [('empty.fasta', '')], ids=_stringify, indirect=["kmer_db_fixture"])
@pytest.mark.parametrize("reads_file", ['empty.fasta', 'tcgaattt.fasta', 'G5012.3.subset.bam'])
@pytest.mark.parametrize("filter_opts", ['', '--readMinOccs 1', '--readMaxOccs 2'])
def test_filter_with_empty_db(kmer_db_fixture, reads_file, filter_opts, tmpdir_function):
    _test_filter_by_kmers(**locals())

@pytest.mark.parametrize("kmer_db_fixture", [('ebola.fasta.gz', '-k 7')], ids=_stringify, indirect=["kmer_db_fixture"])
@pytest.mark.parametrize("reads_file", [pytest.param('G5012.3.testreads.bam', marks=slow_test),
                                        'G5012.3.subset.bam'])
@pytest.mark.parametrize("filter_opts", ['--dbMinOccs 7  --readMinOccs 93',
                                         '--dbMinOccs 4 --readMinOccsFrac .6',
                                         '--readMinOccsFrac .4 --readMaxOccsFrac .55'])
def test_filter_by_kmers(kmer_db_fixture, reads_file, filter_opts, tmpdir_function):
    _test_filter_by_kmers(**locals())

