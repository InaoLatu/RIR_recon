#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import gzip
import time
from multiprocessing import cpu_count, Queue, Process, current_process
import logging

import re
import os.path
from db.model import Block
from db.helper import setup_connection
from netaddr import iprange_to_cidrs
import math

FILELIST = ['afrinic.db.gz', 'apnic.db.inet6num.gz', 'apnic.db.inetnum.gz', 'delegated-lacnic-extended-latest', 'arin.db.gz','ripe.db.inetnum.gz', 'ripe.db.inet6num.gz'] 
NUM_WORKERS = cpu_count()
LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(processName)s - %(message)s'
COMMIT_COUNT = 10000
NUM_BLOCKS = 0

logger = logging.getLogger('create_ripe_db')
logger.setLevel(logging.DEBUG)
formatter = logging.Formatter(LOG_FORMAT)
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(formatter)
stream_handler.setLevel(logging.DEBUG)
logger.addHandler(stream_handler)


def parse_property(block: str, name: str):
    match = re.findall(u'^{0:s}:\s*(.*)$'.format(name), block, re.MULTILINE)
    if match:
        return " ".join(match)
    else:
        return None

def parse_property_inetnum(block: str):
# IPv4
    match = re.findall('^inetnum:[\s]*((?:\d{1,3}\.){3}\d{1,3}[\s]*-[\s]*(?:\d{1,3}\.){3}\d{1,3})', block, re.MULTILINE)
    if match:
        ip_start = re.findall('^inetnum:[\s]*((?:\d{1,3}\.){3}\d{1,3})[\s]*-[\s]*(?:\d{1,3}\.){3}\d{1,3}', block, re.MULTILINE)[0]
        ip_end = re.findall('^inetnum:[\s]*(?:\d{1,3}\.){3}\d{1,3}[\s]*-[\s]*((?:\d{1,3}\.){3}\d{1,3})', block, re.MULTILINE)[0]
        cidrs = iprange_to_cidrs(ip_start, ip_end)
        return '{}'.format(cidrs[0])
# IPv6
    else:
        match = re.findall('^inet6num:[\s]*([0-9a-fA-F:\/]{1,43})', block, re.MULTILINE)
        if match:
            return match[0]
# LACNIC translation for IPv4
        else:
            match = re.findall('^inet4num:[\s]*((?:\d{1,3}\.){3}\d{1,3}/\d{1,2})', block, re.MULTILINE)
            if match:
                return match[0]
            else:
                return None


def read_blocks(filename: str) -> list:
    if filename.endswith('.gz'):
        f = gzip.open(filename, mode='rt', encoding='ISO-8859-1')
    else:
        f = open(filename, mode='rt', encoding='ISO-8859-1')
    single_block = ''
    blocks = []

# Translation for LACNIC DB
    if filename == 'delegated-lacnic-extended-latest':
        for line in f:
            if line.startswith('lacnic'):
                elements = line.split('|')
                if len(elements) >= 7:
                    single_block = ''
                    if elements[2] == 'ipv4':
                        single_block += 'inet4num: ' + elements[3] + '/' + str(int(math.log(4294967296/int(elements[4]),2))) + '\n'
                    elif elements[2] == 'ipv6':
                        single_block += 'inet6num: ' + elements[3] + '/' + elements[4] + '\n'
                    else:
                        continue
                    if len(elements[1]) > 1:
                        single_block += 'country: ' + elements[1] + '\n'
                    if elements[5].isnumeric():
                        single_block += 'last-modified: ' + elements[5] + '\n'
                    single_block += 'descr: ' + elements[6] + '\n'
                    blocks.append(single_block)

# All other DBs goes here
    else:
        for line in f:
            if line.startswith('%') or line.startswith('#') or line.startswith('remarks:') or line.startswith(' '):
                continue
            # block end
            if line.strip() == '':
                if single_block.startswith('inetnum:') or single_block.startswith('inet6num:'):
                    blocks.append(single_block)
                    single_block = ''
                    # comment out to only parse x blocks
                    # if len(blocks) == 100:
                    #    break
                else:
                    single_block = ''
            else:
                single_block += line

    f.close()
    logger.info('Got {} blocks'.format(len(blocks)))
    global NUM_BLOCKS
    NUM_BLOCKS = len(blocks)
    return blocks


def parse_blocks(jobs: Queue, filename: str):
    session = setup_connection()

    counter = 0
    BLOCKS_DONE = 0

    start_time = time.time()
    while True:
        block = jobs.get()
        if block is None:
            break

        inetnum = parse_property_inetnum(block)
        netname = parse_property(block, 'netname')
        description = parse_property(block, 'descr')
        country = parse_property(block, 'country')
        maintained_by = parse_property(block, 'mnt-by')
        created = parse_property(block, 'created')
        last_modified = parse_property(block, 'last-modified')
        source = ""
        if filename == 'delegated-lacnic-extended-latest': 
        	source = 'lacnic'
        else: 
        	source = filename.split('.')[0] #it takes the name of the db from the file name (ripe, apnic...)


        b = Block(inetnum=inetnum, netname=netname, description=description, country=country,
                  maintained_by=maintained_by, created=created, last_modified=last_modified, source=source)

        session.add(b)
        counter += 1
        BLOCKS_DONE += 1
        if counter % COMMIT_COUNT == 0:
            session.commit()
            session.close()
            session = setup_connection()
            logger.debug('committed {} blocks ({} seconds) {:.1f}% done.'.format(counter, round(time.time() - start_time, 2),BLOCKS_DONE * NUM_WORKERS * 100 / NUM_BLOCKS))
            counter = 0
            start_time = time.time()
    session.commit()
    logger.debug('committed last blocks')
    session.close()
    logger.debug('{} finished'.format(current_process().name))


def main():
    overall_start_time = time.time()

    session = setup_connection(create_db=True)

    for FILENAME in FILELIST:
        if os.path.exists(FILENAME):
            logger.info('parsing database file: {}'.format(FILENAME))
            start_time = time.time()
            blocks = read_blocks(FILENAME)
            logger.info('database parsing finished: {} seconds'.format(round(time.time() - start_time, 2)))

            logger.info('parsing blocks')
            start_time = time.time()

            jobs = Queue()

            workers = []
            # start workers
            logger.debug('starting {} processes'.format(NUM_WORKERS))
            for w in range(NUM_WORKERS):
                p = Process(target=parse_blocks, args=(jobs, FILENAME))
                p.start()
                workers.append(p)

            # add tasks
            for b in blocks:
                jobs.put(b)
            for i in range(NUM_WORKERS):
                jobs.put(None)

            # wait to finish
            for p in workers:
                p.join()

            logger.info('block parsing finished: {} seconds'.format(round(time.time() - start_time, 2)))
        else:
            logger.info('File {} not found. Please download using download_dumps.sh'.format(FILENAME))

    logger.info('script finished: {} seconds'.format(round(time.time() - overall_start_time, 2)))


if __name__ == '__main__':
    main()
