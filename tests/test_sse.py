"""SSE 解析与心跳。

心跳这件事只有在"上游憋着不说话"的时候才看得见：思考模型三十秒不出第一个字，
中间层（nginx / 负载均衡 / 浏览器）会先把连接掐掉，而日志里只有一句"客户端断开"。
所以这里单独测它，而不是等着线上遇到。
"""

import time
import unittest

from wb2api import sse


def lines_of(*rows):
    return list(rows)


class ParseTests(unittest.TestCase):
    def test_simple_event(self):
        events = list(sse.iter_sse(lines_of('data: {"a":1}', "")))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].json(), {"a": 1})

    def test_done_stops_iteration(self):
        events = list(sse.iter_sse(lines_of("data: [DONE]", "", "data: {\"late\":1}", "")))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].data, "[DONE]")

    def test_default_has_no_heartbeat(self):
        events = list(sse.iter_sse(lines_of('data: {"a":1}', "")))
        self.assertNotIn(sse.HEARTBEAT_EVENT, [event.event for event in events])

    def test_bytes_lines(self):
        events = list(sse.iter_sse([b'data: {"a":1}', b""]))
        self.assertEqual(events[0].json(), {"a": 1})


class HeartbeatTests(unittest.TestCase):
    def test_heartbeat_while_upstream_is_silent(self):
        def slow():
            yield 'data: {"a":1}'
            yield ""
            time.sleep(0.15)  # 长于心跳间隔
            yield 'data: {"b":2}'
            yield ""

        events = list(sse.iter_sse(slow(), heartbeat=0.05))
        kinds = [event.event for event in events]
        self.assertIn(sse.HEARTBEAT_EVENT, kinds)
        self.assertEqual(kinds[0], "message")
        self.assertEqual(events[0].json(), {"a": 1})
        # 数据不会被心跳吃掉
        payloads = [event.json() for event in events if event.event == "message"]
        self.assertIn({"b": 2}, payloads)

    def test_heartbeat_before_done_still_stops(self):
        def slow():
            yield "data: [DONE]"
            yield ""
            time.sleep(0.2)
            yield 'data: {"late":1}'
            yield ""

        events = list(sse.iter_sse(slow(), heartbeat=0.05))
        self.assertEqual(events[-1].data, "[DONE]")
        self.assertNotIn({"late": 1}, [event.json() for event in events])

    def test_zero_disables(self):
        def slow():
            yield 'data: {"a":1}'
            yield ""

        events = list(sse.iter_sse(slow(), heartbeat=0))
        self.assertEqual([event.event for event in events], ["message"])

    def test_upstream_error_reaches_consumer(self):
        def boom():
            yield 'data: {"a":1}'
            raise RuntimeError("socket died")

        with self.assertRaises(RuntimeError):
            list(sse.iter_sse(boom(), heartbeat=0.05))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
