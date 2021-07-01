from unittest import TestCase
from unittest.mock import Mock
import json
import logging

from enqueue.check_posted_payload import check_posted_payload


class TestPayloadCheck(TestCase):

    def test_blank(self):
        payload_json = ''
        mock_request = Mock()
        mock_request.data = payload_json
        output = check_posted_payload(mock_request, logging)
        expected = False, {
            'error': "No payload found. You must submit a POST request via a DCS webhook notification."
        }
        self.assertEqual(output, expected)

    def test_missing_header(self):
        headers = ''
        payload_json = 'whatever'
        mock_request = Mock()
        mock_request.headers = headers
        mock_request.data = payload_json
        output = check_posted_payload(mock_request, logging)
        expected = False, {
            'error': "This does not appear to be from DCS."
        }
        self.assertEqual(output, expected)

    def test_wrong_header(self):
        headers = {'nonEvent':'whatever'}
        payload_json = 'whatever'
        mock_request = Mock()
        mock_request.headers = headers
        mock_request.data = payload_json
        output = check_posted_payload(mock_request, logging)
        expected = False, {
            'error': "This does not appear to be from DCS."
        }
        self.assertEqual(output, expected)

    def test_bad_header(self):
        headers = {'X-Gitea-Event':'whatever'}
        payload_json = 'whatever'
        mock_request = Mock()
        mock_request.headers = headers
        mock_request.data = payload_json
        output = check_posted_payload(mock_request, logging)
        expected = False, {
            'error': "This does not appear to be a push, release, fork, or delete."
        }
        self.assertEqual(output, expected)

    def test_missing_repo(self):
        headers = {'X-Gitea-Event':'push'}
        payload_json = {
            'repository':{
                'something':'whatever',
                'private':False,
                }
            }
        mock_request = Mock(**{'get_json.return_value':payload_json})
        mock_request.headers = headers
        mock_request.data = payload_json
        output = check_posted_payload(mock_request, logging)
        expected = False, {
            'error': "No repo URL specified for push."
        }
        self.assertEqual(output, expected)

    def test_bad_repo(self):
        headers = {'X-Gitea-Event':'push'}
        payload_json = {
            'repository':{
                'html_url':'whatever',
                'private':False,
                }
            }
        mock_request = Mock(**{'get_json.return_value':payload_json})
        mock_request.headers = headers
        mock_request.data = payload_json
        output = check_posted_payload(mock_request, logging)
        expected = False, {
            'error': "The repo for push does not belong to https://git.door43.org."
        }
        self.assertEqual(output, expected)

    def test_missing_commit_branch(self):
        headers = {'X-Gitea-Event':'push'}
        payload_json = {
            'repository':{
                'html_url':'https://git.door43.org/whatever',
                'private':False,
                }
            }
        mock_request = Mock(**{'get_json.return_value':payload_json})
        mock_request.headers = headers
        mock_request.data = payload_json
        output = check_posted_payload(mock_request, logging)
        expected = False, {
            'error': "No commits specified for push."
        }
        self.assertEqual(output, expected)

    def test_bad_commit_branch(self):
        headers = {'X-Gitea-Event':'push'}
        payload_json = {
            'ref':None,
            'repository':{
                'html_url':'https://git.door43.org/whatever',
                'private':False,
                },
            }
        mock_request = Mock(**{'get_json.return_value':payload_json})
        mock_request.headers = headers
        mock_request.data = payload_json
        output = check_posted_payload(mock_request, logging)
        expected = False, {
            'error': "No commits specified for push."
        }
        self.assertEqual(output, expected)

    def test_missing_default_branch(self):
        headers = {'X-Gitea-Event':'push'}
        payload_json = {
            'ref':'refs/heads/master',
            'repository':{
                'html_url':'https://git.door43.org/whatever',
                'private':False,
                },
            }
        mock_request = Mock(**{'get_json.return_value':payload_json})
        mock_request.headers = headers
        mock_request.data = payload_json
        output = check_posted_payload(mock_request, logging)
        expected = False, {
            'error': "No commits specified for push."
        }
        self.assertEqual(output, expected)

    def test_different_commit_branch(self):
        headers = {'X-Gitea-Event':'push'}
        payload_json = {
            'ref':'refs/heads/notMaster',
            'repository':{
                'html_url':'https://git.door43.org/whatever',
                'default_branch':'master',
                'private':False,
                },
            }
        mock_request = Mock(**{'get_json.return_value':payload_json})
        mock_request.headers = headers
        mock_request.data = payload_json
        output = check_posted_payload(mock_request, logging)
        expected = False, {
            'error': "No commits specified for push."
        }
        self.assertEqual(output, expected)

    def test_missing_commits_entry(self):
        headers = {'X-Gitea-Event':'push'}
        payload_json = {
            'ref':'refs/heads/master',
            'repository':{
                'html_url':'https://git.door43.org/whatever',
                'default_branch':'master',
                'private':False,
                },
            }
        mock_request = Mock(**{'get_json.return_value':payload_json})
        mock_request.headers = headers
        mock_request.data = payload_json
        output = check_posted_payload(mock_request, logging)
        expected = False, {
            'error': "No commits specified for push."
        }
        self.assertEqual(output, expected)

    def test_empty_commits_entry(self):
        headers = {'X-Gitea-Event':'push'}
        payload_json = {
            'ref':'refs/heads/master',
            'repository':{
                'html_url':'https://git.door43.org/whatever',
                'default_branch':'master',
                'private':False,
                },
            'commits': [],
            }
        mock_request = Mock(**{'get_json.return_value':payload_json})
        mock_request.headers = headers
        mock_request.data = payload_json
        output = check_posted_payload(mock_request, logging)
        expected = False, {
            'error': "No commits found for push."
        }
        self.assertEqual(output, expected)

    def test_empty_release(self):
        headers = {'X-Gitea-Event':'release'}
        payload_json = {
            'action': 'published',
            'repository':{
                'html_url':'https://git.door43.org/whatever',
                'private':False,
                },
            }
        mock_request = Mock(**{'get_json.return_value':payload_json})
        mock_request.headers = headers
        mock_request.data = payload_json
        output = check_posted_payload(mock_request, logging)
        expected = True, payload_json
        self.assertEqual(output, expected)

    def test_private_repo(self):
        headers = {'X-Gitea-Event':'push'}
        payload_json = {
            'ref':'refs/heads/master',
            'repository':{
                'html_url':'https://git.door43.org/whatever',
                'default_branch':'master',
                'private':True,
                },
            }
        mock_request = Mock(**{'get_json.return_value':payload_json})
        mock_request.headers = headers
        mock_request.data = payload_json
        output = check_posted_payload(mock_request, logging)
        expected = False, {
            'error': "The repo for push is not public."
        }
        self.assertEqual(output, expected)

    def test_basic_json_success(self):
        headers = {'X-Gitea-Event':'push'}
        payload_json = {
            'ref':'refs/heads/master',
            'repository':{
                'html_url':'https://git.door43.org/whatever',
                'default_branch':'master',
                'private':False,
                },
            'commits': ['some commit info'],
            }
        mock_request = Mock(**{'get_json.return_value':payload_json})
        mock_request.headers = headers
        mock_request.data = payload_json
        output = check_posted_payload(mock_request, logging)
        expected = True, payload_json
        self.assertEqual(output, expected)

    def test_typical_full_json_success(self):
        headers = {'X-Gitea-Event':'push'}
        with open( 'tests/Resources/webhook_post.json', 'rt' ) as json_file:
            payload_json = json.load(json_file)
        mock_request = Mock(**{'get_json.return_value':payload_json})
        mock_request.headers = headers
        mock_request.data = payload_json
        output = check_posted_payload(mock_request, logging)
        expected = True, payload_json
        self.assertEqual(output, expected)
