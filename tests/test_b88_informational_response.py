"""期望：上游 103 临时响应后，应继续读取并转发最终 200 和正文。"""
import unittest
from proxy_regression_support import proxy_exchange


class InformationalResponse(unittest.TestCase):
    def test_multiple_informational_responses_keep_final_errors_and_chunks(self):
        hints = (b'HTTP/1.1 100 Continue\r\n\r\n'
                 b'HTTP/1.1 103 Early Hints\r\nLink: </style.css>\r\n\r\n'
                 b'HTTP/1.1 102 Processing\r\n\r\n')
        final = (b'HTTP/1.1 429 Too Many Requests\r\nRetry-After: 3\r\n'
                 b'Transfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\n\r\n')
        output = proxy_exchange(hints + final)
        self.assertIn(b'HTTP/1.1 429 ', output)
        self.assertIn(b'Retry-After: 3', output)
        self.assertIn(b'{}', output)
        self.assertNotIn(b'103', output)
        self.assertTrue(output.endswith(b'0\r\n\r\n'))

    def test_switching_protocols_and_unfinished_informationals_are_rejected(self):
        for raw in (b'HTTP/1.1 101 Switching Protocols\r\n\r\n',
                    b'HTTP/1.1 103 Early Hints\r\n\r\n',
                    b'HTTP/1.1 103 Early Hints\r\n\r\n' * 34):
            with self.subTest(raw_length=len(raw)):
                self.assertIn(b'HTTP/1.1 502 ', proxy_exchange(raw))

    def test_103_followed_by_200_preserves_final_response(self):
        final = b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Type: application/json\r\n\r\n{}'
        # 对照先证明同一夹具能转发最终响应和标准库支持的 100 Continue。
        for prefix in (b'', b'HTTP/1.1 100 Continue\r\n\r\n'):
            control = proxy_exchange(prefix + final)
            self.assertIn(b'HTTP/1.1 200 ', control)
            self.assertIn(b'{}', control)
        print('controls: direct 200 / 100 then 200 both passed')
        output = proxy_exchange(b'HTTP/1.1 103 Early Hints\r\nLink: </style.css>; rel=preload\r\n\r\n' + final)
        print('wire response =', repr(output))
        self.assertIn(b'HTTP/1.1 200 ', output)
        self.assertIn(b'{}', output)


if __name__ == '__main__':
    unittest.main(verbosity=2)
