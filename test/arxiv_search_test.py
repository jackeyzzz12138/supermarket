"""Offline regression tests; never contact arXiv during CI."""

import argparse
from contextlib import redirect_stderr, redirect_stdout
from email.message import Message
import importlib.util
from io import BytesIO, StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

SCRIPT = Path(__file__).resolve().parents[1] / "registries/memoh/apps/arxiv/skills/arxiv/scripts/search_arxiv.py"
SPEC = importlib.util.spec_from_file_location("search_arxiv", SCRIPT)
arxiv = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(arxiv)

# Synthetic metadata, deliberately including XML escaping and folded whitespace.
FEED = b'''<feed xmlns="http://www.w3.org/2005/Atom"
 xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/"
 xmlns:arxiv="http://arxiv.org/schemas/atom">
 <opensearch:totalResults>42</opensearch:totalResults>
 <opensearch:startIndex>5</opensearch:startIndex>
 <entry><id>http://arxiv.org/abs/2401.01234v2</id>
 <title>Paper\n about A &amp; B</title><summary>An abstract\n with whitespace.</summary>
 <author><name>A. Author</name></author><author><name>B. Author</name></author>
 <published>2024-01-01T00:00:00Z</published><updated>2024-02-01T00:00:00Z</updated>
 <category term="cs.AI"/><category term="cs.CL"/>
 <arxiv:primary_category term="cs.AI"/>
 <arxiv:doi>10.0000/example</arxiv:doi><arxiv:journal_ref>Example 1</arxiv:journal_ref>
 <arxiv:comment>10 pages</arxiv:comment></entry></feed>'''
EMPTY = b'''<feed xmlns="http://www.w3.org/2005/Atom"
 xmlns:o="http://a9.com/-/spec/opensearch/1.1/">
 <o:totalResults>0</o:totalResults><o:startIndex>0</o:startIndex></feed>'''


class QueryTests(unittest.TestCase):
    def test_query_encoding_pagination_and_sort(self):
        params = arxiv.build_params(['--query', 'ti:"A & B" AND cat:cs.AI', '--start', '5',
                                     '--max-results', '5', '--sort-by', 'submittedDate'])
        self.assertEqual(params['search_query'], 'ti:"A & B" AND cat:cs.AI')
        self.assertEqual(params['start'], 5)
        self.assertEqual(params['sortBy'], 'submittedDate')
        self.assertEqual(params['sortOrder'], 'descending')

    def test_inclusive_dates_and_boolean_grouping(self):
        params = arxiv.build_params(['--query', 'ti:a OR ti:b', '--submitted-after', '2024-02-29',
                                     '--submitted-before', '2024-03-01'])
        self.assertEqual(params['search_query'],
                         '(ti:a OR ti:b) AND submittedDate:[202402290000 TO 202403012359]')
        self.assertIn('[199101010000 TO 202403012359]', arxiv.build_params(
            ['--query', 'all:agent', '--submitted-before', '2024-03-01'])['search_query'])
        today = arxiv.datetime.now(arxiv.timezone.utc).date().isoformat().replace('-', '')
        self.assertIn(f'[202403010000 TO {today}2359]', arxiv.build_params(
            ['--query', 'all:agent', '--submitted-after', '2024-03-01'])['search_query'])

    def test_id_normalization_and_deduplication(self):
        for value in ['1706.03762', 'arXiv:1706.03762', 'https://arxiv.org/abs/1706.03762',
                      'https://arxiv.org/pdf/1706.03762.pdf']:
            self.assertEqual(arxiv.normalize_id(value), '1706.03762')
        self.assertEqual(arxiv.normalize_id('hep-th/9901001v1'), 'hep-th/9901001v1')
        params = arxiv.build_params(['--id', '1706.03762', '--id', '1706.03762', '--id', 'hep-th/9901001v1'])
        self.assertEqual(params['id_list'], '1706.03762,hep-th/9901001v1')
        self.assertEqual(params['max_results'], 2)
        self.assertNotIn('search_query', params)

    def test_invalid_ids(self):
        for value in ['../../etc/passwd', 'https://evil.example/abs/1706.03762',
                      'https://arxiv.org/search/1706.03762', '2401.01234v0']:
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                arxiv.normalize_id(value)

    def test_invalid_arguments_fail_before_network(self):
        cases = [[], ['--query', ' '], ['--query', 'all:a', '--max-results', '101'],
                 ['--query', 'all:a', '--start', '-1'], ['--query', 'a', '--id', '1706.03762'],
                 ['--id', '1706.03762', '--submitted-after', '2024-01-01'],
                 ['--query', 'a', '--submitted-after', '2024-02-30'],
                 ['--query', 'a', '--submitted-after', '2024-03-01', '--submitted-before', '2024-01-01']]
        for args in cases:
            with self.subTest(args=args), redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                arxiv.build_params(args)


class AtomTests(unittest.TestCase):
    def test_metadata_and_canonical_links(self):
        result = arxiv.parse_feed(FEED)
        self.assertEqual((result['total_results'], result['start'], result['returned']), (42, 5, 1))
        paper = result['papers'][0]
        self.assertEqual(paper['title'], 'Paper about A & B')
        self.assertEqual(paper['authors'], ['A. Author', 'B. Author'])
        self.assertEqual(paper['abstract'], 'An abstract with whitespace.')
        self.assertEqual(paper['primary_category'], 'cs.AI')
        self.assertEqual(paper['categories'], ['cs.AI', 'cs.CL'])
        self.assertEqual(paper['url'], 'https://arxiv.org/abs/2401.01234v2')
        self.assertEqual(paper['pdf_url'], 'https://arxiv.org/pdf/2401.01234v2')
        self.assertEqual(paper['doi'], '10.0000/example')
        self.assertEqual(paper['comment'], '10 pages')

    def test_empty_results_and_optional_fields(self):
        self.assertEqual(arxiv.parse_feed(EMPTY)['papers'], [])
        minimal = FEED.replace(b'<arxiv:doi>10.0000/example</arxiv:doi>', b'')
        self.assertIsNone(arxiv.parse_feed(minimal)['papers'][0]['doi'])

    def test_api_errors_are_not_papers(self):
        feed = FEED.replace(b'http://arxiv.org/abs/2401.01234v2', b'http://arxiv.org/api/errors#incorrect_id_format')
        with self.assertRaisesRegex(arxiv.ArxivError, 'API error'):
            arxiv.parse_feed(feed)

    def test_malformed_xml_dtd_and_non_atom(self):
        for data in [b'<html/>', b'broken', b'\xff', b'<!DOCTYPE feed><feed/>',
                     FEED.replace(b'<opensearch:totalResults>42</opensearch:totalResults>', b'')]:
            with self.subTest(data=data[:40]), self.assertRaises(arxiv.ArxivError):
                arxiv.parse_feed(data)


def http_error(code, retry_after=None):
    headers = Message()
    if retry_after:
        headers['Retry-After'] = retry_after
    return HTTPError(arxiv.ENDPOINT, code, 'fixture error', headers, BytesIO())


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.clock = arxiv.time.time() + 10
        self.sleeps = []
        self.addCleanup(patch.stopall)
        patch.object(arxiv.time, 'time', side_effect=lambda: self.clock).start()
        patch.object(arxiv.time, 'sleep', side_effect=self.sleep).start()
        self.open = patch.object(arxiv, 'urlopen', side_effect=lambda *a, **k: BytesIO(FEED)).start()
        self.params = arxiv.build_params(['--query', 'ti:"A & B"'])

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.clock += seconds

    def test_cache_and_encoded_https_request(self):
        first = arxiv.fetch(self.params, self.directory)
        second = arxiv.fetch(self.params, self.directory)
        self.assertFalse(first['cache_hit'])
        self.assertTrue(second['cache_hit'])
        self.assertEqual(first['papers'], second['papers'])
        self.assertEqual(self.open.call_count, 1)
        request = self.open.call_args.args[0]
        self.assertTrue(request.full_url.startswith('https://export.arxiv.org/api/query?'))
        self.assertEqual(parse_qs(urlsplit(request.full_url).query)['search_query'], ['ti:"A & B"'])
        self.assertIn('Memoh-arXiv-Search', request.get_header('User-agent'))
        self.assertEqual(self.open.call_args.kwargs['timeout'], 30)

    def test_separate_queries_and_process_invocations_share_throttle(self):
        arxiv.fetch(self.params, self.directory)
        arxiv.fetch(arxiv.build_params(['--query', 'all:other']), self.directory)
        self.assertEqual(self.open.call_count, 2)
        self.assertGreaterEqual(self.sleeps[-1], 3)

    def test_corrupt_and_stale_cache_are_refetched(self):
        arxiv.fetch(self.params, self.directory)
        cached = next(self.directory.glob('*.json'))
        cached.write_text('broken')
        arxiv.fetch(self.params, self.directory)
        self.clock += arxiv.CACHE_TTL + 1
        arxiv.fetch(self.params, self.directory)
        self.assertEqual(self.open.call_count, 3)

    def test_retry_after_and_retry_budget(self):
        self.open.side_effect = [http_error(429, '7'), BytesIO(FEED)]
        arxiv.fetch(self.params, self.directory)
        self.assertIn(7, self.sleeps)
        self.open.side_effect = [http_error(503), http_error(503), http_error(503)]
        with self.assertRaisesRegex(arxiv.ArxivError, 'HTTP 503'):
            arxiv.fetch(arxiv.build_params(['--query', 'all:b']), self.directory)
        self.assertEqual(self.open.call_count, 5)

    def test_long_server_cooldown_persists_across_invocations(self):
        self.open.side_effect = [http_error(429, '120')]
        with self.assertRaisesRegex(arxiv.ArxivError, 'longer cooldown'):
            arxiv.fetch(self.params, self.directory)
        with self.assertRaisesRegex(arxiv.ArxivError, 'cooldown is still active'):
            arxiv.fetch(self.params, self.directory)
        self.assertEqual(self.open.call_count, 1)
        self.assertNotIn(120, self.sleeps)

    def test_final_failed_attempt_preserves_server_cooldown(self):
        self.open.side_effect = [http_error(503), http_error(503), http_error(429, '120')]
        with self.assertRaisesRegex(arxiv.ArxivError, 'longer cooldown'):
            arxiv.fetch(self.params, self.directory)
        with self.assertRaisesRegex(arxiv.ArxivError, 'cooldown is still active'):
            arxiv.fetch(self.params, self.directory)
        self.assertEqual(self.open.call_count, 3)

    def test_bad_requests_and_network_errors_do_not_retry_or_cache(self):
        for error in [http_error(400), URLError('offline'), TimeoutError()]:
            self.open.side_effect = error
            with self.assertRaises(arxiv.ArxivError):
                arxiv.fetch(self.params, self.directory)
        self.assertEqual(self.open.call_count, 3)
        self.assertEqual(list(self.directory.glob('*.json')), [])

    def test_bad_and_oversized_responses_are_not_cached(self):
        self.open.side_effect = [BytesIO(b'<html/>'), BytesIO(b'x' * (arxiv.MAX_BYTES + 1))]
        for _ in range(2):
            with self.assertRaises(arxiv.ArxivError):
                arxiv.fetch(self.params, self.directory)
        self.assertEqual(list(self.directory.glob('*.json')), [])

    def test_retry_after_http_date(self):
        with patch.object(arxiv.time, 'time', return_value=1000):
            self.assertEqual(arxiv.retry_delay('Thu, 01 Jan 1970 00:17:00 GMT', 0), 20)
        self.assertEqual(arxiv.retry_delay('invalid', 1), 6)

    def test_cli_json_and_failure_exit_status(self):
        with patch.dict(arxiv.os.environ, {'ARXIV_CACHE_DIR': str(self.directory)}):
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(arxiv.main(['--query', 'all:a']), 0)
            self.assertEqual(json.loads(output.getvalue())['returned'], 1)
            self.open.side_effect = URLError('offline')
            error = StringIO()
            with redirect_stderr(error), redirect_stdout(StringIO()):
                self.assertEqual(arxiv.main(['--query', 'all:b']), 1)
            self.assertIn('network request failed', error.getvalue())


if __name__ == '__main__':
    unittest.main()
