#!/usr/bin/env python

from io import StringIO
import multiprocessing
import urllib.request
import statistics
import subprocess
import argparse
import tarfile
import logging
import shutil
import glob
import os
import pysam
from Bio import SeqIO
from confindr_wrappers import mash
from confindr_wrappers import bbtools
from Bio.Blast import NCBIXML
from confindr_wrappers import jellyfish
from Bio.Blast.Applications import NcbiblastnCommandline


def write_to_logfile(logfile, out, err, cmd):
    with open(logfile, 'a+') as outfile:
        outfile.write('Command used: {}\n\n'.format(cmd))
        outfile.write('STDOUT: {}\n\n'.format(out))
        outfile.write('STDERR: {}\n\n'.format(err))


def dependency_check(dependency):
    """
    Uses shutil to check if a dependency is installed (won't check version of anything - just presence)
    :param dependency: The dependency as it would be called on the command line (i.e. for blastn, would be blastn)
    :return: True if dependency is present, False if it is not found.
    """
    if shutil.which(dependency) is not None:
        return True
    else:
        return False


def find_paired_reads(fastq_directory, forward_id='_R1', reverse_id='_R2'):
    """
    Looks at a directory to try to find paired fastq files. Should be able to find anything fastq.
    :param fastq_directory: Complete path to directory containing fastq files.
    :param forward_id: Identifier for forward reads. Default R1.
    :param reverse_id: Identifier for reverse reads. Default R2.
    :return: List containing pairs of fastq files, in format [[forward_1, reverse_1], [forward_2, reverse_2]], etc.
    """
    pair_list = list()

    fastq_files = glob.glob(fastq_directory + '/*.f*q*')
    for name in fastq_files:
        if forward_id in name and os.path.isfile(name.replace(forward_id, reverse_id)):
            pair_list.append([name, name.replace(forward_id, reverse_id)])
    return pair_list


def find_unpaired_reads(fastq_directory, forward_id='_R1', reverse_id='_R2'):
    """
    Looks at a directory to find unpaired fastq files.
    :param fastq_directory: Complete path to directory containing fastq files.
    :param forward_id: Identifier for forward reads. Default _R1.
    :param reverse_id: Identifier for forward reads. Default _R2.
    :return: List of files that appear to be unpaired reads.
    """
    read_list = list()
    fastq_files = glob.glob(fastq_directory + '/*.f*q*')
    for name in fastq_files:
        # Iterate through files, adding them to our list of unpaired reads if:
        # 1) They don't have the forward identifier or the reverse identifier in their name.
        # 2) They have forward but the reverse isn't there.
        # 3) They have reverse but the forward isn't there.
        if forward_id not in name and reverse_id not in name:
            read_list.append(name)
        elif forward_id in name and not os.path.isfile(name.replace(forward_id, reverse_id)):
            read_list.append(name)
        elif reverse_id in name and not os.path.isfile(name.replace(reverse_id, forward_id)):
            read_list.append(name)
    return read_list


def find_genusspecific_alleles(profiles_file, target_genus):
    """
    Given a genus name, will parse a custom-made profiles file to find which alleles should be excluded for a target
    genus.
    :param profiles_file: Path to profiles file.
    :param target_genus: Genus that needs to be found.
    :return: List of genes that should be excluded for the genus in question. If the specified genus can't be found
    in the profiles file, list will be empty.
    """
    genes_to_exclude = list()
    with open(profiles_file) as f:
        lines = f.readlines()
    for line in lines:
        line = line.rstrip()
        genus = line.split(':')[0]
        if genus == target_genus:
            genes = line.split(':')[1]
            genes_to_exclude = genes.split(',')
    return genes_to_exclude


def find_genusspecific_allele_list(profiles_file, target_genus):
    """
    A new way of making our specific databases: Make our profiles file have lists of every gene/allele present for
    each genus instead of just excluding a few genes for each. This way, should have much smaller databases
    while managing to make ConFindr a decent bit faster (maybe)
    :param profiles_file: Path to profiles file.
    :param target_genus:
    :return: List of gene/allele combinations that should be part of species-specific database.
    """
    alleles = list()
    with open(profiles_file) as f:
        lines = f.readlines()
    for line in lines:
        line = line.rstrip()
        genus = line.split(':')[0]
        if genus == target_genus:
            alleles = line.split(':')[1].split(',')[:-1]
    return alleles


def setup_allelespecific_database(database_folder, genus, allele_list):
    with open(os.path.join(database_folder, '{}_db.fasta'.format(genus)), 'w') as f:
        sequences = SeqIO.parse(os.path.join(database_folder, 'rMLST_combined.fasta'), 'fasta')
        for item in sequences:
            if item.id in allele_list:
                f.write('>' + item.id + '\n')
                f.write(str(item.seq) + '\n')


def setup_genusspecific_database(database_folder, genus, genes_to_exclude):
    """
    Sets up genus-specific databases (aka databases that exclude genes that are known to have multiple copies, such as
    BACT000060 and BACT000065 for Escherichia)
    :param database_folder: Folder containing rMLST database (will also end up including the genus-specfic database)
    :param genus: Name of genus that database is being created for.
    :param genes_to_exclude: List of genes to exclude for that genus database, generated by find_genusspecific_alleles()
    """
    with open(os.path.join(database_folder, '{}_db.fasta'.format(genus)), 'w') as f:
        sequences = SeqIO.parse(os.path.join(database_folder, 'rMLST_combined.fasta'), 'fasta')
        for item in sequences:
            if item.id.split('_')[0] not in genes_to_exclude:
                f.write('>' + item.id + '\n')
                f.write(str(item.seq) + '\n')


def extract_rmlst_genes(pair, database, forward_out, reverse_out, threads=12, logfile=None):
    """
    Given a pair of reads and an rMLST database, will extract reads that contain sequence from the database.
    :param pair: List containing path to forward reads at index 0 and path to reverse reads at index 1.
    :param database: Path to rMLST database, in FASTA format.
    :param forward_out:
    :param reverse_out:
    :param threads:
    """
    out, err, cmd = bbtools.bbduk_bait(database, pair[0], forward_out, reverse_in=pair[1],
                                       reverse_out=reverse_out, threads=str(threads), returncmd=True)
    if logfile:
        write_to_logfile(logfile, out, err, cmd)


def subsample_reads(forward_in, reverse_in, coverage_level, genome_size, forward_out, reverse_out,
                    threads=12, logfile=None):
    """
    Will subsample reads to a desired coverage level, given the coverage level and genome size.
    :param forward_in: Forward input reads.
    :param reverse_in: Reverse input reads.
    :param coverage_level: Desired coverage depth, as an int.
    :param genome_size: Estimated genome size, as an int.
    :param forward_out: Forward output reads.
    :param reverse_out: Reverse output reads.
    :param threads: Number of threads to use.
    """
    bases_target = coverage_level * genome_size
    out, err, cmd = bbtools.subsample_reads(forward_in, forward_out, bases_target, reverse_in=reverse_in,
                                            reverse_out=reverse_out, returncmd=True, threads=str(threads))
    if logfile:
        write_to_logfile(logfile, out, err, cmd)


def generate_kmers(forward_reads, reverse_reads, counts_file, kmer_size, tmpdir, logfile=None):
    """
    Generates a set of kmers given a set of forward and reverse reads using jellyfish.
    Output will be a fasta-formatted file, with the title of each kmer being its count.
    :param forward_reads: Path to forward input reads.
    :param reverse_reads: Path to reverse input reads.
    :param counts_file: Output fasta file.
    :param kmer_size: Kmer size, should be an int.
    :param tmpdir: Temporary directory where intermediary files get stored. Deleted when method finishes.
    """
    if not os.path.isdir(tmpdir):
        os.makedirs(tmpdir)
    out, err, cmd = jellyfish.count(forward_reads, reverse_in=reverse_reads, count_file=os.path.join(tmpdir, 'mer_counts.jf'),
                                    kmer_size=kmer_size, options='--bf-size 100M', returncmd=True)
    if logfile:
        write_to_logfile(logfile, out, err, cmd)
    jellyfish.dump(os.path.join(tmpdir, 'mer_counts.jf'), counts_file)  # TODO: Add logging for this too.
    shutil.rmtree(tmpdir)


def rename_kmers(input_kmers, output_kmers, cutoff):
    """
    Given a fasta-formatted kmer count file generated by jellyfish, renames the kmers so that
    the names for each are unique in format >count_uniqueid
    :param input_kmers: Path to Kmers created by jellyfish (the generate_kmers() method works to make these)
    :param output_kmers: Path to output location. Can be the same as input location if you want to overwrite things.
    :param cutoff: Kmers must appear at least this many times to make it into the renamed file.
    :return: Total number of unique kmers present, as an int.
    """
    with open(input_kmers) as f:
        fastas = f.readlines()

    num_mers = 0
    sequences = list()
    for i in range(len(fastas)):
        if '>' in fastas[i]:
            if int(fastas[i].replace('>', '')) >= cutoff:
                num_mers += 1
                sequences.append(fastas[i].rstrip() + '_' + str(num_mers) + '\n' + fastas[i + 1])
    # Write out our solid kmers to file to be used later.
    with open(output_kmers, 'w') as f:
        f.write(''.join(sequences))

    return num_mers


def parse_bamfile(bamfile, kmer_size):
    """
    Parses a bamfile to find sequences with one mismatch to other sequences.
    :param bamfile: Path to bamfile.
    :param kmer_size: Kmer size, used to make sure hits are full length.
    :return: List of fasta ids that have one mismatch to some other sequence.
    """
    mismatch_kmer_headers = list()
    bam_handle = pysam.AlignmentFile(bamfile, 'rb')
    for match in bam_handle:
        if match.cigarstring is not None:
            if '1X' in match.cigarstring and match.query_alignment_length == kmer_size:
                mismatch_kmer_headers.append(bam_handle.getrname(match.reference_id))
    return mismatch_kmer_headers


def check_db_presence(database):
    """
    Checks if a BLAST nucleotide database is present via checking for files with the proper extensions.
    :param database: Path to Fasta file for database.
    :return: True is all necessary files are present, False if not.
    """
    extensions = ['.nhr', '.nin', '.nsq']
    is_present = True
    for extension in extensions:
        if not os.path.isfile(database + extension):
            is_present = False
    return is_present


def make_blast_database(database, logfile='log.txt'):
    """
    Makes a nucleotide blast database.
    :param database: Path to fasta file you want to turn into a database.
    :param logfile: Logfile to write things to.
    """
    cmd = 'makeblastdb -in {} -dbtype nucl'.format(database)
    with open(logfile, 'a+') as f:
        f.write('Command: {}\n'.format(cmd))
        subprocess.call(cmd, shell=True, stdout=f, stderr=f)


def present_in_db(query_sequence, database, kmer_size):
    """
    Given a query sequence, will determine if the sequence has a hit in a specified blast database.
    :param query_sequence: Sequence to query against the blast database as a string.
    :param database: Path to BLAST database.
    :param kmer_size: The length of the sequence to query. Ensures that full-length hits are present.
    :return: True if sequence is in database, False if sequence is not in database.
    """
    # Blast the sequence against our database.
    blastn = NcbiblastnCommandline(db=database, outfmt=5)
    stdout, stderr = blastn(stdin=query_sequence)
    # If there's any full-length result, the sequence is present. No result means not present.
    if stdout:
        for record in NCBIXML.parse(StringIO(stdout)):
            for alignment in record.alignments:
                for hsp in alignment.hsps:
                    if hsp.align_length == kmer_size:
                        return True
                    else:
                        return False
        # Sometimes despite something being in stdout there aren't any records to iterate through.
        # Not how I thought it worked, but apparently the case.
        return False
    # Given that apparently stdout always gets created, I don't think this is actually reachable,
    # but it's left here just in case I've totally misunderstood how things work.
    else:
        return False


def find_cross_contamination(databases, pair, tmpdir='tmp', log='log.txt', threads=1):
    """
    Usese mash to find out whether or not a sample has more than one genus present, indicating cross-contamination.
    :param databases: A databases folder, which must contain refseq.msh, a mash sketch that has one representative
    per genus from refseq.
    :param tmpdir: Temporary directory to store mash result files in.
    :param log: Logfile to write to.
    :param threads: Number of threads to run mash wit.
    :return: cross_contam: a bool that is True if more than one genus is found, and False otherwise.
    :return: genera_present: A string. If only one genus is found, string is just genus. If more than one genus is found,
    the string is a list of genera present, separated by colons (i.e. for Escherichia and Salmonella found, string would
    be 'Escherichia:Salmonella'. If no genus found, return 'NA'
    """
    genera_present = list()
    out, err, cmd = mash.screen('{}/refseq.msh'.format(databases), pair[0],
                                pair[1], threads=threads, w='', i='0.95',
                                output_file=os.path.join(tmpdir, 'screen.tab'), returncmd=True)
    write_to_logfile(log, out, err, cmd)
    screen_output = mash.read_mash_screen(os.path.join(tmpdir, 'screen.tab'))
    for item in screen_output:
        mash_genus = item.query_id.split('/')[-3]
        if mash_genus == 'Shigella':
            mash_genus = 'Escherichia'
        if mash_genus not in genera_present:
            genera_present.append(mash_genus)
    if len(genera_present) == 1:
        genera_present = genera_present[0]
    elif len(genera_present) == 0:
        genera_present = 'NA'
    else:
        tmpstr = ''
        for mash_genus in genera_present:
            tmpstr += mash_genus + ':'
        genera_present = tmpstr[:-1]
    return genera_present


def find_cross_contamination_unpaired(databases, reads, tmpdir='tmp', log='log.txt', threads=1):
    """
    Usese mash to find out whether or not a sample has more than one genus present, indicating cross-contamination.
    :param databases: A databases folder, which must contain refseq.msh, a mash sketch that has one representative
    per genus from refseq.
    :param tmpdir: Temporary directory to store mash result files in.
    :param log: Logfile to write to.
    :param threads: Number of threads to run mash wit.
    :return: cross_contam: a bool that is True if more than one genus is found, and False otherwise.
    :return: genera_present: A string. If only one genus is found, string is NA. If more than one genus is found,
    the string is a list of genera present, separated by colons (i.e. for Escherichia and Salmonella found, string would
    be 'Escherichia:Salmonella'
    """
    genera_present = list()
    out, err, cmd = mash.screen('{}/refseq.msh'.format(databases), reads,
                                threads=threads, w='', i='0.95',
                                output_file=os.path.join(tmpdir, 'screen.tab'), returncmd=True)
    write_to_logfile(log, out, err, cmd)
    screen_output = mash.read_mash_screen(os.path.join(tmpdir, 'screen.tab'))
    for item in screen_output:
        mash_genus = item.query_id.split('/')[-3]
        if mash_genus == 'Shigella':
            mash_genus = 'Escherichia'
        if mash_genus not in genera_present:
            genera_present.append(mash_genus)
    if len(genera_present) == 1:
        genera_present = genera_present[0]
    elif len(genera_present) == 0:
        genera_present = 'NA'
    else:
        tmpstr = ''
        for mash_genus in genera_present:
            tmpstr += mash_genus + ':'
        genera_present = tmpstr[:-1]
    return genera_present


def has_two_high_quality_bases(list_of_scores):
    """
    Finds if a site has at least two bases of very high quality (>35), enough that it can be considered
    fairly safe to say that base is actually there.
    :param list_of_scores: List of quality scores as integers.
    :return: True if site has at least two bases >= 35 phred score, false otherwise
    """
    # TODO: This is a very inelegant way of doing this. Refine.
    quality_bases_count = 0
    for score in list_of_scores:
        if score >= 35:
            quality_bases_count += 1
        if quality_bases_count >= 2:
            return True
    return False


def find_if_multibase(column):
    """
    Finds if a position in a pileup has more than one base present.
    :param column: A pileupColumn generated by pysam
    :return: If position has more than one base, a dictionary with counts for the bases. Otherwise, returns
    empty dictionary
    """
    base_dict = dict()
    # Sometimes the qualities come out to ridiculously high (>70) values. Looks to be because sometimes reads
    # are overlapping and the qualities get summed for overlapping bases. Issue opened on pysam.
    base_qualities = dict()
    for read in column.pileups:
        if read.query_position is not None:  # Not entirely sure why this is sometimes None, but it causes bad stuff
            base = read.alignment.query_sequence[read.query_position]
            quality = read.alignment.query_qualities[read.query_position]
            if base not in base_qualities:
                base_qualities[base] = [quality]
            else:
                base_qualities[base].append(quality)
            if base in base_dict:
                base_dict[base] += 1
            else:
                base_dict[base] = 1
    # Assume that things that are truly indicative of fast evolution at least 10 percent of population?
    # Assume things that only have count of 1 are sequencing errors.
    appropriate_ratio = True
    if len(base_dict) == 1:
        appropriate_ratio = False
    # for base in base_dict:
    #     if base_dict[base]/column.n >= 0.9 or base_dict[base]/column.n <= 0.1 or base_dict[base] == 1:
    #         appropriate_ratio = False
    if appropriate_ratio is False:
        base_dict = dict()
    # At this point we know that bases are in an appropriate(ish) ratio.
    # Now check that at least two bases for each of the bases present are very high (>35) quality.
    if base_dict:
        high_quality_bases = True
        for base in base_qualities:
            if has_two_high_quality_bases(base_qualities[base]) is False:
                high_quality_bases = False
        if high_quality_bases is False:
            base_dict = dict()
    return base_dict


def get_contig_names(fasta_file):
    """
    Gets contig names from a fasta file using SeqIO.
    :param fasta_file: Full path to uncompressed, fasta-formatted file
    :return: List of contig names.
    """
    contig_names = list()
    for contig in SeqIO.parse(fasta_file, 'fasta'):
        contig_names.append(contig.id)
    return contig_names


def read_contig(contig_name, bamfile_name, reference_fasta):
    bamfile = pysam.AlignmentFile(bamfile_name, 'rb')
    multibase_position_dict = dict()
    # THese parameters seem to be fairly undocumented with pysam, but I think that they should make the output
    # that I'm getting to match up with what I'm seeing in Tablet.
    for column in bamfile.pileup(contig_name,
                                 stepper='samtools', ignore_orphans=False, fastafile=pysam.FastaFile(reference_fasta),
                                 min_base_quality=0):
        base_dict = find_if_multibase(column)
        if base_dict:
            if column.reference_name in multibase_position_dict:
                multibase_position_dict[column.reference_name].append(column.pos)
            else:
                multibase_position_dict[column.reference_name] = [column.pos]
    return multibase_position_dict


def find_contamination(pair, args):
    log = os.path.join(args.output_name, 'confindr_log.txt')
    snv_list = list()
    max_kmers = 0
    sample_name = os.path.split(pair[0])[-1].split(args.forward_id)[0]
    sample_tmp_dir = os.path.join(args.output_name, sample_name)
    if not os.path.isdir(sample_tmp_dir):
        os.makedirs(sample_tmp_dir)
    logging.info('Checking for cross-species contamination...')
    genus = find_cross_contamination(args.databases, pair, tmpdir=sample_tmp_dir, log=log, threads=args.threads)
    if len(genus.split(':')) > 1:
        snv_list = [0]
        write_output(output_report=os.path.join(args.output_name, 'confindr_report.csv'),
                     sample_name=sample_name,
                     snv_list=snv_list,
                     genus=genus,
                     max_kmers=max_kmers)
        logging.info('Found cross-contamination! Skipping rest of analysis...')
        shutil.rmtree(sample_tmp_dir)
        return
    # Main method for finding contamination - works on one pair at a time.
    # Need to:
    # Setup genus-specific databases, if necessary.
    if genus != 'NA':
        sample_database = os.path.join(args.databases, '{}_db.fasta'.format(genus))
        if not os.path.isfile(os.path.join(args.databases, '{}_db.fasta'.format(genus))):
            logging.info('Setting up genus-specific database for genus {}...'.format(genus))
            allele_list = find_genusspecific_allele_list(os.path.join(args.databases, 'gene_allele.txt'), genus)
            setup_allelespecific_database(args.databases, genus, allele_list)
    else:
        sample_database = os.path.join(args.databases, 'rMLST_combined.fasta')
    # Extract rMLST reads and quality trim.
    logging.info('Extracting rMLST genes...')
    extract_rmlst_genes(pair, sample_database,
                        forward_out=os.path.join(sample_tmp_dir, 'rmlst_R1.fastq.gz'),
                        reverse_out=os.path.join(sample_tmp_dir, 'rmlst_R2.fastq.gz'),
                        threads=args.threads, logfile=log)
    logging.info('Quality trimming...')
    out, err, cmd = bbtools.bbduk_trim(forward_in=os.path.join(sample_tmp_dir, 'rmlst_R1.fastq.gz'),
                                       reverse_in=os.path.join(sample_tmp_dir, 'rmlst_R2.fastq.gz'),
                                       forward_out=os.path.join(sample_tmp_dir, 'trimmed_R1.fastq.gz'),
                                       reverse_out=os.path.join(sample_tmp_dir, 'trimmed_R2.fastq.gz'),
                                       threads=str(args.threads), returncmd=True)
    logging.debug('Quality trim command used was: {}'.format(cmd))
    write_to_logfile(log, out, err, cmd)
    # Now do the actual contamination detection cycle the number of times specified by arguments.
    # At this point, should just map things back to database.
    cmd = 'samtools faidx {}'.format(os.path.join(args.databases, genus + '_db.fasta'))
    os.system(cmd)
    cmd = 'bbmap.sh ref={ref} in={forward_in} in2={reverse_in} out={outbam} ' \
          'nodisk ambig=all'.format(ref=os.path.join(args.databases, genus + '_db.fasta'),
                                    forward_in=os.path.join(sample_tmp_dir, 'trimmed_R1.fastq.gz'),
                                    reverse_in=os.path.join(sample_tmp_dir, 'trimmed_R2.fastq.gz'),
                                    outbam=os.path.join(sample_tmp_dir, 'out.bam'))
    os.system(cmd)
    cmd = 'samtools sort {inbam} -o {sorted_bam}'.format(inbam=os.path.join(sample_tmp_dir, 'out.bam'),
                                                         sorted_bam=os.path.join(sample_tmp_dir, 'sorted.bam'))
    os.system(cmd)
    cmd = 'samtools index {sorted_bam}'.format(sorted_bam=os.path.join(sample_tmp_dir, 'sorted.bam'))
    os.system(cmd)

    # Find which rMLST allele has the most reads mapped back to it for each gene.
    gene_alleles_to_use = dict()
    bamfile = pysam.AlignmentFile(os.path.join(sample_tmp_dir, 'sorted.bam'), 'rb')
    contig_names = get_contig_names(fasta_file=os.path.join(args.databases, '{}_db.fasta'.format(genus)))
    for contig in contig_names:
        gene = contig.split('_')[0]
        allele = contig.split('_')[1]
        if gene not in gene_alleles_to_use:
            gene_alleles_to_use[gene] = [allele, bamfile.count(contig=contig)]
        else:
            read_count = bamfile.count(contig=contig)
            if gene_alleles_to_use[gene][1] < read_count:
                gene_alleles_to_use[gene] = [allele, read_count]
    # Extract only the genes with most reads aligned, then align reads to those.
    gene_alleles = list()
    for gene in gene_alleles_to_use:
        contig_name = gene + '_' + gene_alleles_to_use[gene][0]
        gene_alleles.append(contig_name)

    with open(os.path.join(sample_tmp_dir, 'rmlst.fasta'), 'w') as f:
        for contig in SeqIO.parse(os.path.join(args.databases, '{}_db.fasta'.format(genus)), 'fasta'):
            if contig.id in gene_alleles:
                f.write('>{}\n'.format(contig.id))
                f.write(str(contig.seq) + '\n')

    cmd = 'samtools faidx {}'.format(os.path.join(sample_tmp_dir, 'rmlst.fasta'))
    os.system(cmd)
    cmd = 'bbmap.sh ref={ref} in={forward_in} in2={reverse_in} out={outbam} ' \
          'nodisk'.format(ref=os.path.join(sample_tmp_dir, 'rmlst.fasta'),
                          forward_in=os.path.join(sample_tmp_dir, 'trimmed_R1.fastq.gz'),
                          reverse_in=os.path.join(sample_tmp_dir, 'trimmed_R2.fastq.gz'),
                          outbam=os.path.join(sample_tmp_dir, 'out_2.bam'))
    os.system(cmd)
    cmd = 'samtools sort {inbam} -o {sorted_bam}'.format(inbam=os.path.join(sample_tmp_dir, 'out_2.bam'),
                                                         sorted_bam=os.path.join(sample_tmp_dir, 'sorted_2.bam'))
    os.system(cmd)
    cmd = 'samtools index {sorted_bam}'.format(sorted_bam=os.path.join(sample_tmp_dir, 'sorted_2.bam'))
    os.system(cmd)
    # Now find number of multi-positions for each rMLST gene/allele combination
    multi_positions = 0
    for gene in gene_alleles_to_use:
        contig_name = gene + '_' + gene_alleles_to_use[gene][0]
        multibase_position_dict = read_contig(contig_name=contig_name,
                                              bamfile_name=os.path.join(sample_tmp_dir, 'sorted_2.bam'),
                                              reference_fasta=os.path.join(sample_tmp_dir, 'rmlst.fasta'))
        multi_positions += len(multibase_position_dict)
        # print(contig_name)
        # print(multibase_position_dict)
    snv_list = [multi_positions]
    write_output(output_report=os.path.join(args.output_name, 'confindr_report.csv'),
                 sample_name=sample_name,
                 snv_list=snv_list,
                 genus=genus,
                 max_kmers=max_kmers)
    shutil.rmtree(sample_tmp_dir)

    """
    logging.info('Beginning {} cycles of contamination detection...'.format(args.number_subsamples))
    for i in range(args.number_subsamples):
        logging.info('Working on cycle {} of {}...'.format(i + 1, args.number_subsamples))
        # Subsample
        subsample_reads(forward_in=os.path.join(sample_tmp_dir, 'trimmed_R1.fastq.gz'),
                        reverse_in=os.path.join(sample_tmp_dir, 'trimmed_R2.fastq.gz'),
                        coverage_level=args.subsample_depth,
                        genome_size=35000,  # This is the sum of the longest allele for each rMLST gene.
                        forward_out=os.path.join(sample_tmp_dir, 'subsample_{}_R1.fastq.gz'.format(str(i))),
                        reverse_out=os.path.join(sample_tmp_dir, 'subsample_{}_R2.fastq.gz'.format(str(i))),
                        threads=args.threads, logfile=log)
        # Kmerize subsampled reads.
        generate_kmers(forward_reads=os.path.join(sample_tmp_dir, 'subsample_{}_R1.fastq.gz'.format(str(i))),
                       reverse_reads=os.path.join(sample_tmp_dir, 'subsample_{}_R2.fastq.gz'.format(str(i))),
                       counts_file=os.path.join(sample_tmp_dir, 'kmer_counts_{}.fasta'.format(str(i))),
                       kmer_size=args.kmer_size,
                       tmpdir=os.path.join(sample_tmp_dir, 'tmp'), logfile=log)
        # Rename kmers so each has a unique ID, and count the number of kmers.
        num_kmers = rename_kmers(input_kmers=os.path.join(sample_tmp_dir, 'kmer_counts_{}.fasta'.format(str(i))),
                                 output_kmers=os.path.join(sample_tmp_dir, 'kmer_counts_{}.fasta'.format(str(i))),
                                 cutoff=args.kmer_cutoff)
        logging.debug('Found {} kmers'.format(num_kmers))
        if num_kmers > max_kmers:
            max_kmers = num_kmers
        elif num_kmers == 0:
            continue
        # Find mismatches.

        # Step 1 of mismatch finding: Run bbmap with the kmer file.
        out, err, cmd = bbtools.bbmap(reference=os.path.join(sample_tmp_dir, 'kmer_counts_{}.fasta'.format(str(i))),
                                      forward_in=os.path.join(sample_tmp_dir, 'kmer_counts_{}.fasta'.format(str(i))),
                                      ambig='all',
                                      overwrite='true',
                                      out_bam=os.path.join(sample_tmp_dir, 'subsample_{}.bam'.format(str(i))),
                                      threads=str(args.threads), returncmd=True)
        logging.debug('BBmap command used was {}'.format(cmd))
        write_to_logfile(log, out, err, cmd)

        # Step 2 of mismatch finding: Parse the bamfile created by bbmap to find one mismatch kmers.
        fasta_ids = parse_bamfile(os.path.join(sample_tmp_dir, 'subsample_{}.bam'.format(str(i))), args.kmer_size)
        # Step 2.5: Create a dictionary so you know which ID goes with which sequence.
        mer_dict = dict()
        with open(os.path.join(sample_tmp_dir, 'kmer_counts_{}.fasta'.format(str(i)))) as f:
            mers = f.readlines()
        for j in range(0, len(mers), 2):
            key = mers[j].replace('>', '')
            key = key.replace('\n', '')
            mer_dict[key] = mers[j + 1]

        # Step 3 of mismatch finding: Blast kmers found from parsing against database to make sure they're
        # not overhangs into non-RMLST regions that could cause false positives.
        # First part of this step: Check that the blast database is actually present. If it isn't, make one.
        if not check_db_presence(sample_database):
            make_blast_database(sample_database, logfile=log)
        # Now set up the blast.
        # Create list of sequences to blast.
        to_blast = list()
        for fasta_id in fasta_ids:
            to_blast.append(mer_dict[fasta_id])
        # Setup the multiprocessing pool.
        pool = multiprocessing.Pool(processes=args.threads)
        db_list = [sample_database] * len(fasta_ids)
        kmer_size_list = [args.kmer_size] * len(fasta_ids)
        results = pool.starmap(present_in_db, zip(to_blast, db_list, kmer_size_list))
        pool.close()
        pool.join()
        snv_count = 0
        for result in results:
            if result:
                snv_count += 1
        logging.debug('Found {} contaminating SNVs...'.format(snv_count))
        snv_list.append(snv_count)

    # Create contamination report.
    logging.debug('Writing output to {}'.format(os.path.join(args.output_name, 'confindr_report.csv')))
    write_output(output_report=os.path.join(args.output_name, 'confindr_report.csv'),
                 sample_name=sample_name,
                 snv_list=snv_list,
                 genus=genus,
                 max_kmers=max_kmers)
    logging.debug('Removing temporary directory {}'.format(sample_tmp_dir))
    shutil.rmtree(sample_tmp_dir)
    logging.info('Finished analysis of sample {}!'.format(sample_name))
    """


def write_output(output_report, sample_name, snv_list, genus, max_kmers):
    if len(snv_list) == 0:
        snv_list.append(0)
    if statistics.median(snv_list) > 2 or len(genus.split(':')) > 1 or max_kmers > 45000:
        contaminated = True
    else:
        contaminated = False
    with open(output_report, 'a+') as f:
        f.write('{samplename},{genus},{numcontamsnvs},{numuniquekmers},'
                '{contamstatus}\n'.format(samplename=sample_name,
                                          genus=genus,
                                          numcontamsnvs=statistics.median(snv_list),
                                          numuniquekmers=max_kmers,
                                          contamstatus=contaminated))


def find_contamination_unpaired(args, reads):
    # Setup log file.
    log = os.path.join(args.output_name, 'confindr_log.txt')
    # snv_list and max_kmers will be used later for contam detection - get them set up here.
    snv_list = list()
    max_kmers = 0
    # Setup a sample name - may want to improve this at some point, currently takes everything before the .fastq.gz
    sample_name = os.path.split(reads)[-1].split('.')[0]
    sample_tmp_dir = os.path.join(args.output_name, sample_name)
    if not os.path.isdir(sample_tmp_dir):
        os.makedirs(sample_tmp_dir)
    logging.info('Checking for cross-species contamination...')
    genus = find_cross_contamination_unpaired(args.databases, reads, tmpdir=sample_tmp_dir, log=log, threads=args.threads)
    if len(genus.split(':')) > 1:
        snv_list = [0]
        write_output(output_report=os.path.join(args.output_name, 'confindr_report.csv'),
                     sample_name=sample_name,
                     snv_list=snv_list,
                     genus=genus,
                     max_kmers=max_kmers)
        logging.info('Found cross-contamination! Skipping rest of analysis...')
        shutil.rmtree(sample_tmp_dir)
        return
    # Setup a genusspecfic database, if necessary.
    if genus != 'NA':
        sample_database = os.path.join(args.databases, '{}_db.fasta'.format(genus))
        if not os.path.isfile(os.path.join(args.databases, '{}_db.fasta'.format(genus))):
            logging.info('Setting up genus-specific database for genus {}...'.format(genus))
            allele_list = find_genusspecific_allele_list(os.path.join(args.databases, 'gene_allele.txt'), genus)
            setup_allelespecific_database(args.databases, genus, allele_list)
    else:
        sample_database = os.path.join(args.databases, 'rMLST_combined.fasta')
    # Get tmpdir for this sample created.
    sample_tmp_dir = os.path.join(args.output_name, sample_name)
    if not os.path.isdir(sample_tmp_dir):
        os.makedirs(sample_tmp_dir)
    # With everything set up, time to start the workflow.
    # First thing to do: Extract rMLST genes.
    logging.info('Extracting rMLST genes...')
    out, err, cmd = bbtools.bbduk_bait(reference=sample_database, forward_in=reads,
                                       forward_out=os.path.join(sample_tmp_dir, 'rmlst.fastq.gz'),
                                       returncmd=True, threads=args.threads)
    logging.debug('rMLST extraction command used: {}'.format(cmd))
    write_to_logfile(log, out, err, cmd)
    logging.info('Quality trimming...')
    # With rMLST genes extracted, get our quality trimming done.
    out, err, cmd = bbtools.bbduk_trim(forward_in=os.path.join(sample_tmp_dir, 'rmlst.fastq.gz'),
                                       forward_out=os.path.join(sample_tmp_dir, 'trimmed.fastq.gz'),
                                       returncmd=True, threads=args.threads)
    logging.debug('Quality trim command used: {}'.format(cmd))
    write_to_logfile(log, out, err, cmd)
    # Now we go through our contamination detection cycle the number of times specified.
    for i in range(args.number_subsamples):
        logging.info('Working on cycle {} of {}...'.format(i + 1, args.number_subsamples))
        # Find number of bases we need to subsample.
        num_bases = 35000 * args.subsample_depth
        out, err, cmd = bbtools.subsample_reads(forward_in=os.path.join(sample_tmp_dir, 'trimmed.fastq.gz'),
                                                forward_out=os.path.join(sample_tmp_dir, 'subsample_{}.fastq'.format(str(i))),
                                                num_bases=num_bases, returncmd=True,
                                                threads=args.threads)
        write_to_logfile(log, out, err, cmd)
        # Kmerize the reads using jellyfish.
        out, err, cmd = jellyfish.count(forward_in=os.path.join(sample_tmp_dir, 'subsample_{}.fastq'.format(str(i))),
                                        count_file=os.path.join(sample_tmp_dir, 'mer_counts.jf'),
                                        options='--bf-size 100M', kmer_size=args.kmer_size, returncmd=True)
        write_to_logfile(log, out, err, cmd)
        out, err, cmd = jellyfish.dump(os.path.join(sample_tmp_dir, 'mer_counts.jf'),
                                       os.path.join(sample_tmp_dir, 'kmer_counts_{}.fasta'.format(str(i))),
                                       returncmd=True)
        write_to_logfile(log, out, err, cmd)
        # With jellyfish done, rename our kmers.
        num_kmers = rename_kmers(input_kmers=os.path.join(sample_tmp_dir, 'kmer_counts_{}.fasta'.format(str(i))),
                                 output_kmers=os.path.join(sample_tmp_dir, 'kmer_counts_{}.fasta'.format(str(i))),
                                 cutoff=args.kmer_cutoff)
        # Update the maximum kmer count.
        if num_kmers == 0:
            continue
        elif num_kmers > max_kmers:
            max_kmers = num_kmers
        # Now find mismatches.
        # Step 1 of mismatch finding: Run bbmap with the kmer file.
        out, err, cmd = bbtools.bbmap(reference=os.path.join(sample_tmp_dir, 'kmer_counts_{}.fasta'.format(str(i))),
                                      forward_in=os.path.join(sample_tmp_dir, 'kmer_counts_{}.fasta'.format(str(i))),
                                      ambig='all',
                                      overwrite='true',
                                      out_bam=os.path.join(sample_tmp_dir, 'subsample_{}.bam'.format(str(i))),
                                      threads=str(args.threads), returncmd=True)
        logging.debug('BBMap command used: {}'.format(cmd))
        write_to_logfile(log, out, err, cmd)

        # Step 2 of mismatch finding: Parse the bamfile created by bbmap to find one mismatch kmers.
        fasta_ids = parse_bamfile(os.path.join(sample_tmp_dir, 'subsample_{}.bam'.format(str(i))), args.kmer_size)
        # Step 2.5: Create a dictionary so you know which ID goes with which sequence.
        mer_dict = dict()
        with open(os.path.join(sample_tmp_dir, 'kmer_counts_{}.fasta'.format(str(i)))) as f:
            mers = f.readlines()
        for j in range(0, len(mers), 2):
            key = mers[j].replace('>', '')
            key = key.replace('\n', '')
            mer_dict[key] = mers[j + 1]

        # Step 3 of mismatch finding: Blast kmers found from parsing against database to make sure they're
        # not overhangs into non-RMLST regions that could cause false positives.
        # First part of this step: Check that the blast database is actually present. If it isn't, make one.
        if not check_db_presence(sample_database):
            make_blast_database(sample_database, logfile=log)
        # Now set up the blast.
        # Create list of sequences to blast.
        to_blast = list()
        for fasta_id in fasta_ids:
            to_blast.append(mer_dict[fasta_id])
        # Setup the multiprocessing pool.
        pool = multiprocessing.Pool(processes=args.threads)
        db_list = [sample_database] * len(fasta_ids)
        kmer_size_list = [args.kmer_size] * len(fasta_ids)
        results = pool.starmap(present_in_db, zip(to_blast, db_list, kmer_size_list))
        pool.close()
        pool.join()
        snv_count = 0
        for result in results:
            if result:
                snv_count += 1
        logging.debug('Found {} contaminating SNVs...'.format(snv_count))
        snv_list.append(snv_count)

    logging.debug('Writing output to {}'.format(os.path.join(args.output_name, 'confindr_report.csv')))
    write_output(output_report=os.path.join(args.output_name, 'confindr_report.csv'),
                 sample_name=sample_name,
                 snv_list=snv_list,
                 genus=genus,
                 max_kmers=max_kmers)

    logging.debug('Removing temporary directory {}'.format(sample_tmp_dir))
    shutil.rmtree(sample_tmp_dir)
    logging.info('Finished analysis of sample {}!'.format(sample_name))


def check_for_databases_and_download(database_location, tmpdir):
    # Check for the files necessary - should have rMLST_combined.fasta, gene_allele.txt, profiles.txt, and refseq.msh
    necessary_files = ['rMLST_combined.fasta', 'gene_allele.txt', 'profiles.txt', 'refseq.msh']
    all_files_present = True
    for necessary_file in necessary_files:
        if not os.path.isfile(os.path.join(database_location, necessary_file)):
            all_files_present = False

    if not all_files_present:
        logging.info('Databases not present. Downloading to {}. This may take a few minutes...'.format(database_location))
        if not os.path.isdir(tmpdir):
            os.makedirs(tmpdir)
        # Download
        # TODO: Progress bar?
        urllib.request.urlretrieve('https://ndownloader.figshare.com/files/11864267', os.path.join(tmpdir, 'confindr_db.tar.gz'))
        # Extract.
        tar = tarfile.open(os.path.join(tmpdir, 'confindr_db.tar.gz'))
        tar.extractall(path=database_location)
        tar.close()
        # We now have the files extracted to database_location/databases - move them to database_location
        for item in glob.glob(os.path.join(database_location, 'databases', '*')):
            shutil.move(item, database_location)
        # Cleanup!
        shutil.rmtree(os.path.join(database_location, 'databases'))
        os.remove(os.path.join(tmpdir, 'confindr_db.tar.gz'))
        logging.info('Databases successfully downloaded...')


if __name__ == '__main__':
    version = 'ConFindr 0.3.4'
    cpu_count = multiprocessing.cpu_count()
    parser = argparse.ArgumentParser()
    parser.add_argument('-i', '--input_directory',
                        type=str,
                        required=True,
                        help="Folder that contains fastq files you want to check for contamination. "
                             "Will find any fastq file that contains .fq or .fastq in the filename.")
    parser.add_argument('-o', '--output_name',
                        type=str,
                        required=True,
                        help='Base name for output/temporary directories.')
    parser.add_argument('-d', '--databases',
                        type=str,
                        default=os.environ.get('CONFINDR_DB', os.path.expanduser('~/.confindr_db')),
                        help='Databases folder. If you don\'t already have databases, they will be downloaded '
                             'automatically. You may also specify the full path to the databases.')
    parser.add_argument('-t', '--threads',
                        type=int,
                        default=cpu_count,
                        help='Number of threads to run analysis with.')
    parser.add_argument('-n', '--number_subsamples',
                        type=int,
                        default=3,
                        help='Number of times to subsample. Default is 3, which should be sufficient.')
    parser.add_argument('-k', '--kmer-size',
                        type=int,
                        default=31,
                        help='Kmer size to use for contamination detection.')
    parser.add_argument('-s', '--subsample_depth',
                        type=int,
                        default=20,
                        help='Depth to subsample to. Higher increases sensitivity, but also false positive '
                             'rate. Default is 20.')
    parser.add_argument('-c', '--kmer_cutoff',
                        type=int,
                        default=3,
                        help='Number of times you need to see a kmer before it is considered trustworthy.'
                             ' Kmers with counts below this number will be discarded. Default is 3, which provides'
                             ' a good trade-off between sensitivity and specificity.')
    parser.add_argument('-fid', '--forward_id',
                        type=str,
                        default='_R1',
                        help='Identifier for forward reads.')
    parser.add_argument('-rid', '--reverse_id',
                        type=str,
                        default='_R2',
                        help='Identifier for reverse reads.')
    parser.add_argument('-v', '--version',
                        action='version',
                        version=version)
    parser.add_argument('-verbosity', '--verbosity',
                        choices=['debug', 'info', 'warning'],
                        default='info',
                        help='Amount of output you want printed to the screen. Defaults to info, which should be good '
                             'for most users.')

    args = parser.parse_args()
    # Setup the logger. TODO: Different colors for different levels.
    if args.verbosity == 'info':
        logging.basicConfig(format='\033[92m \033[1m %(asctime)s \033[0m %(message)s ',
                            level=logging.INFO,
                            datefmt='%Y-%m-%d %H:%M:%S')
    elif args.verbosity == 'debug':
        logging.basicConfig(format='\033[92m \033[1m %(asctime)s \033[0m %(message)s ',
                            level=logging.DEBUG,
                            datefmt='%Y-%m-%d %H:%M:%S')
    elif args.verbosity == 'warning':
        logging.basicConfig(format='\033[92m \033[1m %(asctime)s \033[0m %(message)s ',
                            level=logging.WARNING,
                            datefmt='%Y-%m-%d %H:%M:%S')
    # Check for dependencies.
    logging.info('Welcome to {version}! Beginning analysis of your samples...'.format(version=version))
    all_dependencies_present = True
    dependencies = ['jellyfish', 'bbmap.sh', 'bbduk.sh', 'blastn', 'mash', 'reformat.sh']
    for dependency in dependencies:
        if dependency_check(dependency) is False:
            logging.error('Dependency {} not found. Please make sure it is installed and present'
                          ' on your $PATH.'.format(dependency))
            all_dependencies_present = False
    if not all_dependencies_present:
        quit(code=1)

    # Make the output directory.
    if not os.path.isdir(args.output_name):
        os.makedirs(args.output_name)

    # Check if databases necessary to run are present, and download them if they aren't
    check_for_databases_and_download(database_location=args.databases,
                                     tmpdir=args.output_name)

    # Open the output report file.
    with open(os.path.join(args.output_name, 'confindr_report.csv'), 'w') as f:
        f.write('Sample,Genus,NumContamSNVs,NumUniqueKmers,ContamStatus\n')
    # Figure out what pairs of reads, as well as unpaired reads, are present.
    paired_reads = find_paired_reads(args.input_directory, forward_id=args.forward_id, reverse_id=args.reverse_id)
    unpaired_reads = find_unpaired_reads(args.input_directory, forward_id=args.forward_id, reverse_id=args.reverse_id)
    # Process paired reads, one sample at a time.
    for pair in paired_reads:
        sample_name = os.path.split(pair[0])[-1].split(args.forward_id)[0]
        logging.info('Beginning analysis of sample {}...'.format(sample_name))
        try:
            find_contamination(pair, args)
        except subprocess.CalledProcessError:
            # If something unforeseen goes wrong, traceback will be printed to screen.
            # We then add the sample to the report with a note that it failed.
            snv_list = [0]
            genus = 'Error processing sample'
            max_kmers = 0
            write_output(output_report=os.path.join(args.output_name, 'confindr_report.csv'),
                         sample_name=sample_name,
                         snv_list=snv_list,
                         genus=genus,
                         max_kmers=max_kmers)
            logging.warning('Encountered error when attempting to run ConFindr on sample '
                            '{sample}. Skipping...'.format(sample=sample_name))
            shutil.rmtree(os.path.join(args.output_name, sample_name))
    # Process unpaired reads, also one sample at a time.
    for reads in unpaired_reads:
        sample_name = os.path.split(reads)[-1].split('.')[0]
        logging.info('Beginning analysis of sample {}...'.format(sample_name))
        try:
            find_contamination_unpaired(args, reads)
        except subprocess.CalledProcessError:
            # If something unforeseen goes wrong, traceback will be printed to screen.
            # We then add the sample to the report with a note that it failed.
            snv_list = [0]
            genus = 'Error processing sample'
            max_kmers = 0
            write_output(output_report=os.path.join(args.output_name, 'confindr_report.csv'),
                         sample_name=sample_name,
                         snv_list=snv_list,
                         genus=genus,
                         max_kmers=max_kmers)
            logging.warning('Encountered error when attempting to run ConFindr on sample '
                            '{sample}. Skipping...'.format(sample=sample_name))
            shutil.rmtree(os.path.join(args.output_name, sample_name))

    logging.info('Contamination detection complete!')