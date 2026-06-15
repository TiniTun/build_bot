import unittest

from core.session_state import repair_tool_call_pairing


def _assistant(tool_ids, content="thinking"):
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [
            {"id": tid, "type": "function", "function": {"name": "x", "arguments": "{}"}}
            for tid in tool_ids
        ],
    }


def _tool(tid, content="ok"):
    return {"role": "tool", "tool_call_id": tid, "content": content}


class RepairToolCallPairingTests(unittest.TestCase):
    def test_well_formed_history_is_unchanged(self):
        messages = [
            {"role": "user", "content": "hi"},
            _assistant(["a"]),
            _tool("a"),
            {"role": "assistant", "content": "done"},
        ]
        self.assertEqual(repair_tool_call_pairing(messages), messages)

    def test_dangling_assistant_tool_calls_gets_placeholder_result(self):
        # Process died after persisting the assistant message but before results.
        messages = [
            {"role": "user", "content": "hi"},
            _assistant(["a"]),
        ]
        repaired = repair_tool_call_pairing(messages)
        self.assertEqual(repaired[-1]["role"], "tool")
        self.assertEqual(repaired[-1]["tool_call_id"], "a")
        self.assertIn("unavailable", repaired[-1]["content"])

    def test_partial_results_are_filled_in(self):
        messages = [
            _assistant(["a", "b"]),
            _tool("a"),
        ]
        repaired = repair_tool_call_pairing(messages)
        tool_ids = [m["tool_call_id"] for m in repaired if m["role"] == "tool"]
        self.assertEqual(tool_ids, ["a", "b"])

    def test_results_are_reordered_to_match_call_order(self):
        messages = [
            _assistant(["a", "b"]),
            _tool("b", "second"),
            _tool("a", "first"),
        ]
        repaired = repair_tool_call_pairing(messages)
        tools = [m for m in repaired if m["role"] == "tool"]
        self.assertEqual([t["tool_call_id"] for t in tools], ["a", "b"])

    def test_orphan_tool_message_is_dropped(self):
        # Compaction kept the tail starting mid-run, dropping the assistant call.
        messages = [
            {"role": "user", "content": "[summary]"},
            _tool("a"),
            {"role": "assistant", "content": "done"},
        ]
        repaired = repair_tool_call_pairing(messages)
        self.assertNotIn("tool", [m["role"] for m in repaired])

    def test_duplicate_tool_results_collapsed(self):
        messages = [
            _assistant(["a"]),
            _tool("a", "first"),
            _tool("a", "dup"),
        ]
        repaired = repair_tool_call_pairing(messages)
        tools = [m for m in repaired if m["role"] == "tool"]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["content"], "first")


if __name__ == "__main__":
    unittest.main()
