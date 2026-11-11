"""Lỗi provider phải giữ lại bằng chứng đã thu thập, kể cả qua CLI và eval."""
import io
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'evals'))
from agent import run_agent
import run
import run_evals
from status_tools import StatusTools
from test_lab import final, tool_call


class ProviderErrorTests(unittest.TestCase):
    def test_provider_errors_preserve_trace_and_usage(self):
        errors = [RuntimeError('Provider trả HTTP 503.'), TimeoutError('Đọc quá hạn'),
                  KeyError('choices'), IndexError('list index out of range'),
                  ValueError('Phản hồi không hợp lệ'), json.JSONDecodeError('Invalid JSON', '{', 1)]
        for error in errors:
            for completed_steps in (0, 2):
                with self.subTest(error=type(error).__name__, completed_steps=completed_steps):
                    responses = [tool_call(str(i), 'get_status', '{"provider":"github"}')
                                 for i in range(completed_steps)]
                    for i, response in enumerate(responses):
                        response['usage'] = {'total_tokens': 10 + i}
                    provider = Mock()
                    provider.complete.side_effect = responses + [error, final('Không được gọi lại')]

                    result = run_agent('Kiểm tra GitHub', provider, StatusTools())

                    self.assertEqual(result['status'], 'error')
                    self.assertEqual(result['answer'], '')
                    self.assertIn(type(error).__name__, result['error'])
                    self.assertIn(str(error), result['error'])
                    self.assertEqual(result['usage'], [r['usage'] for r in responses])
                    self.assertEqual(result['trace'][0], {'event': 'user_question', 'content': 'Kiểm tra GitHub'})
                    observations = result['trace'][1:-1]
                    self.assertEqual(len(observations), completed_steps)
                    for i, observation in enumerate(observations):
                        self.assertEqual(observation, {
                            'event': 'tool', 'step': i, 'name': 'get_status',
                            'arguments': {'provider': 'github'},
                            'result': StatusTools().execute('get_status', {'provider': 'github'}),
                        })
                    self.assertEqual(result['trace'][-1], {
                        'event': 'error', 'step': completed_steps, 'message': result['error'],
                    })
                    self.assertEqual(provider.complete.call_count, completed_steps + 1)

    def test_error_after_source_revision_preserves_validation_trace(self):
        provider = Mock()
        provider.complete.side_effect = [
            tool_call('1', 'get_incident_details', '{"provider":"github","incident_id":"xr3myzj7ms51"}'),
            final('Thiếu nguồn'), RuntimeError('Không kết nối được provider'),
        ]
        result = run_agent('Kiểm tra', provider, StatusTools())
        self.assertEqual(result['status'], 'error')
        self.assertEqual([event['event'] for event in result['trace']],
                         ['user_question', 'tool', 'validation_missing_source', 'error'])
        self.assertEqual(result['trace'][-1]['step'], 2)
        self.assertEqual(result['usage'], [{}, {}])

    def test_unexpected_provider_errors_and_interrupts_propagate(self):
        for error in (TypeError('Lỗi lập trình'), KeyboardInterrupt(), SystemExit(1)):
            with self.subTest(error=type(error).__name__):
                provider = Mock()
                provider.complete.side_effect = error
                with self.assertRaises(type(error)):
                    run_agent('Kiểm tra', provider, StatusTools())

    def test_tool_execution_errors_are_outside_provider_handler(self):
        provider = Mock()
        provider.complete.return_value = tool_call('1', 'get_status', '{"provider":"github"}')
        tools = Mock()
        tools.execute.side_effect = RuntimeError('Lỗi thực thi tool')
        with self.assertRaisesRegex(RuntimeError, 'Lỗi thực thi tool'):
            run_agent('Kiểm tra', provider, tools)


class ProviderErrorRunnerTests(unittest.TestCase):
    def first_response(self):
        return {'choices': [{'message': tool_call('1', 'get_status', '{"provider":"github"}')['message'],
                             'finish_reason': 'tool_calls'}], 'usage': {'total_tokens': 12}}

    @patch.dict('os.environ', {'OPENAI_API_KEY': 'dummy'})
    @patch('providers.post_json')
    def test_cli_prints_json_error_with_trace_and_usage(self, post):
        post.side_effect = [self.first_response(), TimeoutError('Đọc quá hạn')]
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, 'argv', ['run.py', 'Kiểm tra', '--provider', 'openai', '--model', 'test']), \
                patch('sys.stdout', stdout), patch('sys.stderr', stderr):
            exit_code = run.main()
        result = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(stderr.getvalue(), '')
        self.assertEqual(result['status'], 'error')
        self.assertEqual([event['event'] for event in result['trace']], ['user_question', 'tool', 'error'])
        self.assertEqual(result['usage'], [{'total_tokens': 12}])
        self.assertIn('TimeoutError', result['error'])

    @patch.dict('os.environ', {'OPENAI_API_KEY': 'dummy'})
    @patch('providers.post_json')
    def test_eval_preserves_trace_and_usage_on_provider_error(self, post):
        post.side_effect = [self.first_response(), RuntimeError('Provider trả HTTP 503.')]
        args = SimpleNamespace(provider='openai', model='test', judge_provider='openai',
                               judge_model='test', max_steps=6)
        case = {'id': 'X', 'question': 'Kiểm tra', 'expected': 'Dùng tool'}
        item = run_evals.run_case(case, args)
        self.assertEqual(item['result']['status'], 'error')
        self.assertEqual([event['event'] for event in item['result']['trace']], ['user_question', 'tool', 'error'])
        self.assertEqual(item['result']['usage'], [{'total_tokens': 12}])
        self.assertIn('RuntimeError', item['result']['error'])
        self.assertEqual(item['grade']['status'], 'unscored')


if __name__ == '__main__':
    unittest.main()
