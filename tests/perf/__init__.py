"""Perf test harness: latency + token comparison across LLM models.

Gated by ENABLE_LLM_PERF=true. Independent of the eval rig's behavior
contracts — measures only latency, token counts, and tail latency.
"""
