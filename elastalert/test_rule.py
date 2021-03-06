#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import absolute_import
from __future__ import print_function

import copy
import datetime
import logging
import random
import re
import string
import sys

import argparse
import mock
import simplejson
import yaml

from elastalert.config import load_modules
from elastalert.config import load_options
from elastalert.elastalert import ElastAlerter
from elastalert.util import lookup_es_key
from elastalert.util import ts_now
from elastalert.util import ts_to_dt

logging.getLogger().setLevel(logging.INFO)
logging.getLogger('elasticsearch').setLevel(logging.WARNING)


def print_terms(terms, parent):
    """ Prints a list of flattened dictionary keys """
    for term in terms:
        if type(terms[term]) != dict:
            print('\t' + parent + term)
        else:
            print_terms(terms[term], parent + term + '.')


class MockElastAlerter(object):
    def __init__(self):
        self.data = []

    def test_file(self, conf, args):
        """ Loads a rule config file, performs a query over the last day (args.days), lists available keys
        and prints the number of results. """
        load_options(conf, {})
        print("Successfully loaded %s\n" % (conf['name']))

        if args.schema_only:
            return []

        # Set up elasticsearch client and query
        es_config = ElastAlerter.build_es_conn_config(conf)
        es_client = ElastAlerter.new_elasticsearch(es_config)
        start_time = ts_now() - datetime.timedelta(days=args.days)
        end_time = ts_now()
        ts = conf.get('timestamp_field', '@timestamp')
        query = ElastAlerter.get_query(conf['filter'], starttime=start_time, endtime=end_time, timestamp_field=ts)
        index = ElastAlerter.get_index(conf, start_time, end_time)

        # Get one document for schema
        try:
            res = es_client.search(index, size=1, body=query, ignore_unavailable=True)
        except Exception as e:
            print("Error running your filter:", file=sys.stderr)
            print(repr(e)[:2048], file=sys.stderr)
            return None
        num_hits = len(res['hits']['hits'])
        if not num_hits:
            return []

        terms = res['hits']['hits'][0]['_source']
        doc_type = res['hits']['hits'][0]['_type']

        # Get a count of all docs
        count_query = ElastAlerter.get_query(conf['filter'], starttime=start_time, endtime=end_time, timestamp_field=ts, sort=False)
        count_query = {'query': {'filtered': count_query}}
        try:
            res = es_client.count(index, doc_type=doc_type, body=count_query, ignore_unavailable=True)
        except Exception as e:
            print("Error querying Elasticsearch:", file=sys.stderr)
            print(repr(e)[:2048], file=sys.stderr)
            return None

        num_hits = res['count']
        print("Got %s hits from the last %s day%s" % (num_hits, args.days, 's' if args.days > 1 else ''))
        print("\nAvailable terms in first hit:")
        print_terms(terms, '')

        # Check for missing keys
        pk = conf.get('primary_key')
        ck = conf.get('compare_key')
        if pk and not lookup_es_key(terms, pk):
            print("Warning: primary key %s is either missing or null!", file=sys.stderr)
        if ck and not lookup_es_key(terms, ck):
            print("Warning: compare key %s is either missing or null!", file=sys.stderr)

        include = conf.get('include')
        if include:
            for term in include:
                if not lookup_es_key(terms, term) and '*' not in term:
                    print("Included term %s may be missing or null" % (term), file=sys.stderr)

        for term in conf.get('top_count_keys', []):
            # If the index starts with 'logstash', fields with .raw will be available but won't in _source
            if term not in terms and not (term.endswith('.raw') and term[:-4] in terms and index.startswith('logstash')):
                print("top_count_key %s may be missing" % (term), file=sys.stderr)
        print('')  # Newline

        # Download up to 10,000 documents to save
        if args.save and not args.count:
            try:
                res = es_client.search(index, size=10000, body=query, ignore_unavailable=True)
            except Exception as e:
                print("Error running your filter:", file=sys.stderr)
                print(repr(e)[:2048], file=sys.stderr)
                return None
            num_hits = len(res['hits']['hits'])
            print("Downloaded %s documents to save" % (num_hits))
            return res['hits']['hits']

        return None

    def mock_count(self, rule, start, end, index):
        """ Mocks the effects of get_hits_count using global data instead of Elasticsearch """
        count = 0
        for doc in self.data:
            if start <= ts_to_dt(doc[rule['timestamp_field']]) < end:
                count += 1
        return {end: count}

    def mock_hits(self, rule, start, end, index):
        """ Mocks the effects of get_hits using global data instead of Elasticsearch. """
        docs = []
        for doc in self.data:
            if start <= ts_to_dt(doc[rule['timestamp_field']]) < end:
                docs.append(doc)

        # Remove all fields which don't match 'include'
        for doc in docs:
            fields_to_remove = []
            for field in doc:
                if field != '_id':
                    if not any([re.match(incl.replace('*', '.*'), field) for incl in rule['include']]):
                        fields_to_remove.append(field)
            map(doc.pop, fields_to_remove)

        # Separate _source and _id, convert timestamps
        resp = [{'_source': doc, '_id': doc['_id']} for doc in docs]
        for doc in resp:
            doc['_source'].pop('_id')
        ElastAlerter.process_hits(rule, resp)
        return resp

    def mock_terms(self, rule, start, end, index, key, qk=None, size=None):
        """ Mocks the effects of get_hits_terms using global data instead of Elasticsearch. """
        if key.endswith('.raw'):
            key = key[:-4]
        buckets = {}
        for doc in self.data:
            if key not in doc:
                continue
            if start <= ts_to_dt(doc[rule['timestamp_field']]) < end:
                if qk is None or doc[rule['query_key']] == qk:
                    buckets.setdefault(doc[key], 0)
                    buckets[doc[key]] += 1
        counts = buckets.items()
        counts.sort(key=lambda x: x[1], reverse=True)
        if size:
            counts = counts[:size]
        buckets = [{'key': value, 'doc_count': count} for value, count in counts]
        return {end: buckets}

    def mock_elastalert(self, elastalert):
        """ Replaces elastalert's get_hits functions with mocks. """
        elastalert.get_hits_count = self.mock_count
        elastalert.get_hits_terms = self.mock_terms
        elastalert.get_hits = self.mock_hits
        elastalert.new_elasticsearch = mock.Mock()

    def run_elastalert(self, rule, args):
        """
        Creates an ElastAlert instance and run's over for a specific rule using either real or mock data.
        returns a list of calls arguments for writeback function
        """
        # Mock configuration. Nothing here is used except run_every
        conf = {'rules_folder': 'rules',
                'run_every': datetime.timedelta(minutes=5),
                'buffer_time': datetime.timedelta(minutes=45),
                'alert_time_limit': datetime.timedelta(hours=24),
                'es_host': 'es',
                'es_port': 14900,
                'writeback_index': 'wb',
                'max_query_size': 100000,
                'old_query_limit': datetime.timedelta(weeks=1),
                'disable_rules_on_error': False}

        # Load and instantiate rule
        load_options(rule, conf)
        load_modules(rule)
        conf['rules'] = [rule]

        # If using mock data, make sure it's sorted and find appropriate time range
        timestamp_field = rule.get('timestamp_field', '@timestamp')
        if args.json:
            if not self.data:
                return
            try:
                self.data.sort(key=lambda x: x[timestamp_field])
                starttime = ts_to_dt(self.data[0][timestamp_field])
                endtime = self.data[-1][timestamp_field]
                endtime = ts_to_dt(endtime) + datetime.timedelta(seconds=1)
            except KeyError as e:
                print("All documents must have a timestamp and _id: %s" % (e), file=sys.stderr)
                return

            # Create mock _id for documents if it's missing
            used_ids = []

            def get_id():
                _id = ''.join([random.choice(string.letters) for i in range(16)])
                if _id in used_ids:
                    return get_id()
                used_ids.append(_id)
                return _id

            for doc in self.data:
                doc.update({'_id': doc.get('_id', get_id())})
        else:
            endtime = ts_now()
            starttime = endtime - datetime.timedelta(days=args.days)

        # Set run_every to cover the entire time range unless use_count_query or use_terms_query is set
        # This is to prevent query segmenting which unnecessarily slows down tests
        if not rule.get('use_terms_query') and not rule.get('use_count_query'):
            conf['run_every'] = endtime - starttime

        # Instantiate ElastAlert to use mock config and special rule
        with mock.patch('elastalert.elastalert.get_rule_hashes'):
            with mock.patch('elastalert.elastalert.load_rules') as load_conf:
                load_conf.return_value = conf
                if args.alert:
                    client = ElastAlerter(['--verbose'])
                else:
                    client = ElastAlerter(['--debug'])

        # Replace get_hits_* functions to use mock data
        if args.json:
            self.mock_elastalert(client)

        # Mock writeback for both real data and json data
        client.writeback_es = None
        with mock.patch.object(client, 'writeback') as mock_writeback:
            client.run_rule(rule, endtime, starttime)

            if mock_writeback.call_count:
                print("\nWould have written the following documents to elastalert_status:\n")
                for call in mock_writeback.call_args_list:
                    print("%s - %s\n" % (call[0][0], call[0][1]))

            return mock_writeback.call_args_list

    def run_rule_test(self):
        """ Uses args to run the various components of MockElastAlerter such as loading the file, saving data, loading data, and running. """
        parser = argparse.ArgumentParser(description='Validate a rule configuration')
        parser.add_argument('file', metavar='rule', type=str, help='rule configuration filename')
        parser.add_argument('--schema-only', action='store_true', help='Show only schema errors; do not run query')
        parser.add_argument('--days', type=int, default=1, action='store', help='Query the previous N days with this rule')
        parser.add_argument('--data', type=str, metavar='FILENAME', action='store', dest='json', help='A JSON file containing data to run the rule against')
        parser.add_argument('--alert', action='store_true', help='Use actual alerts instead of debug output')
        parser.add_argument('--save-json', type=str, metavar='FILENAME', action='store', dest='save', help='A file to which documents from the last day or --days will be saved')
        parser.add_argument('--count-only', action='store_true', dest='count', help='Only display the number of documents matching the filter')
        args = parser.parse_args()

        with open(args.file) as fh:
            rule_yaml = yaml.load(fh)

        if args.json:
            with open(args.json, 'r') as data_file:
                self.data = simplejson.loads(data_file.read())
        else:
            hits = self.test_file(copy.deepcopy(rule_yaml), args)
            if hits and args.save:
                with open(args.save, 'wb') as data_file:
                    # Add _id to _source for dump
                    [doc['_source'].update({'_id': doc['_id']}) for doc in hits]
                    data_file.write(simplejson.dumps([doc['_source'] for doc in hits], indent='    '))
        if not args.schema_only and not args.count:
            self.run_elastalert(rule_yaml, args)


def main():
    test_instance = MockElastAlerter()
    test_instance.run_rule_test()

if __name__ == '__main__':
    main()
