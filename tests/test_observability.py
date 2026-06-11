"""Tests for ADR-014 observability: structured logging, subprocess timeouts,
silent exception upgrades.

Test budget: ~5 smoke tests per the Architect's guidance.
"""

from __future__ import annotations

import ast
import logging
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch


class TestMCPServerLogging:
    """Layer 1: MCP server logging setup."""

    def test_run_configures_logging_to_stderr(self):
        """Verify run() calls logging.basicConfig targeting stderr with INFO level."""
        import axiom_graph.mcp_server as mod

        with patch.object(mod, "mcp") as mock_mcp, patch("logging.basicConfig") as mock_basic:
            mock_mcp.run = MagicMock()
            mod.run()
            mock_basic.assert_called_once()
            call_kwargs = mock_basic.call_args
            import sys

            assert call_kwargs.kwargs.get("stream") is sys.stderr
            assert call_kwargs.kwargs.get("level") == logging.INFO

    def test_run_respects_log_level_env_var(self):
        """Verify AXIOM_GRAPH_LOG_LEVEL env var overrides the default level."""
        import axiom_graph.mcp_server as mod

        with (
            patch.object(mod, "mcp") as mock_mcp,
            patch("logging.basicConfig") as mock_basic,
            patch.dict(os.environ, {"AXIOM_GRAPH_LOG_LEVEL": "DEBUG"}),
        ):
            mock_mcp.run = MagicMock()
            mod.run()
            mock_basic.assert_called_once()
            call_kwargs = mock_basic.call_args
            assert call_kwargs.kwargs.get("level") == logging.DEBUG


class TestToolTiming:
    """Layer 1: Tool function timing via _timed_tool decorator."""

    def test_timed_tool_emits_start_and_done(self):
        """Verify _timed_tool decorator emits start and done log lines."""
        from axiom_graph.mcp_server import _timed_tool

        logger = logging.getLogger("axiom_graph.mcp._helpers")

        @_timed_tool
        def fake_tool():
            return "ok"

        with patch.object(logger, "info") as mock_info:
            result = fake_tool()
            assert result == "ok"
            calls = [str(c) for c in mock_info.call_args_list]
            call_text = " ".join(calls)
            assert "start" in call_text
            assert "done" in call_text


class TestSubprocessTimeout:
    """Layer 2: Subprocess timeout handling."""

    def test_diff_subprocess_has_timeout(self):
        """Verify all subprocess.run calls in lifecycle/api.py have explicit timeout."""
        diff_path = Path(__file__).parent.parent / "axiom_graph" / "lifecycle" / "api.py"
        source = diff_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == "run"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "subprocess"
                ):
                    kwarg_names = [kw.arg for kw in node.keywords]
                    assert "timeout" in kwarg_names, f"subprocess.run at line {node.lineno} missing timeout"


class TestExceptionLogging:
    """Layer 3: Silent exception upgrades."""

    def test_git_utils_get_git_sha_logs_on_failure(self):
        """Verify get_git_sha logs instead of bare pass on exception."""
        from axiom_graph.index.git_utils import get_git_sha

        logger = logging.getLogger("axiom_graph.index.git_utils")
        with patch.object(logger, "debug") as mock_debug:
            result = get_git_sha(Path("/nonexistent/path/that/should/not/exist"))
            assert result is None
            assert mock_debug.call_count >= 1


class TestTimedToolWarning:
    """US-1: _timed_tool logs WARNING with exception message on error."""

    def test_timed_tool_error_uses_warning_level(self):
        """Verify _timed_tool source uses logger.warning (not logger.info) on exception."""
        helpers_path = Path(__file__).parent.parent / "axiom_graph" / "mcp" / "_helpers.py"
        source = helpers_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        # Find the _timed_tool function
        timed_tool_func = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_timed_tool":
                timed_tool_func = node
                break
        assert timed_tool_func is not None, "_timed_tool function not found"

        # Find all logger.warning and logger.info calls within _timed_tool
        warning_calls = []
        info_error_calls = []
        for node in ast.walk(timed_tool_func):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if isinstance(node.func.value, ast.Name) and node.func.value.id == "logger":
                    if node.func.attr == "warning":
                        # Check if the format string contains "error"
                        if node.args and isinstance(node.args[0], ast.Constant):
                            if "error" in str(node.args[0].value):
                                warning_calls.append(node)
                    elif node.func.attr == "info":
                        if node.args and isinstance(node.args[0], ast.Constant):
                            if "error" in str(node.args[0].value):
                                info_error_calls.append(node)

        assert len(warning_calls) >= 1, "Expected at least one logger.warning call with 'error' in _timed_tool"
        assert len(info_error_calls) == 0, "Found logger.info with 'error' in _timed_tool -- should be logger.warning"

    def test_timed_tool_error_includes_exception_message(self):
        """Verify _timed_tool captures exception with 'as exc' and includes it in log."""
        helpers_path = Path(__file__).parent.parent / "axiom_graph" / "mcp" / "_helpers.py"
        source = helpers_path.read_text(encoding="utf-8")
        # The except clause should capture the exception and the log should reference it
        assert "except Exception as exc:" in source, "Expected 'except Exception as exc:' in _timed_tool"
        # The warning log should include the exception variable
        assert 'logger.warning("tool %s: error' in source, "Expected logger.warning with error format in _timed_tool"


class TestBatchInlineLoop:
    """US-2: Batch tools use inline loops with per-item error handling."""

    def test_axiom_graph_source_batch_partial_failure(self, tmp_path):
        """Verify axiom_graph_source batch mode returns per-item errors without aborting."""
        from axiom_graph.index import db as db_mod
        from axiom_graph.models import AxiomNode

        db_path = tmp_path / ".axiom_graph" / "graph.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db_mod.init_db(db_path)

        # Create a source file and a node pointing to it
        src = tmp_path / "mod.py"
        src.write_text("x = 1\n", encoding="utf-8")
        node = AxiomNode(
            id="test::mod",
            node_type="composite_process",
            title="mod",
            location="mod.py",
            source="ast",
            code_hash="abc123",
            level_0="mod",
            level_1="mod module",
            level_2="",
            level_3_location="mod.py",
        )
        db_mod.upsert_node(db_path, node)

        from axiom_graph.query.mcp_tools import axiom_graph_source

        result = axiom_graph_source(
            str(tmp_path),
            node_id="ignored",
            node_ids=["test::mod", "test::nonexistent"],
        )
        # Should contain both results separated by ---
        assert "---" in result
        # First should succeed, second should be an error
        parts = result.split("---")
        assert "x = 1" in parts[0]
        assert "ERROR" in parts[1]

    def test_axiom_graph_graph_batch_no_self_recursion_timing(self, tmp_path):
        """Verify batch axiom_graph_graph uses inline loop (no self-recursive calls to _timed_tool)."""
        # This test verifies the structural change by checking that batch
        # mode wraps each item in try/except (the ERROR output pattern)
        from axiom_graph.index import db as db_mod

        db_path = tmp_path / ".axiom_graph" / "graph.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db_mod.init_db(db_path)

        from axiom_graph.query.mcp_tools import axiom_graph_graph

        result = axiom_graph_graph(
            str(tmp_path),
            node_id="ignored",
            node_ids=["nonexistent_a", "nonexistent_b"],
        )
        # Both should produce ERROR, separated by ---
        parts = result.split("---")
        assert len(parts) == 2
        assert "ERROR" in parts[0]
        assert "ERROR" in parts[1]


class TestDirectionValidation:
    """US-4: axiom_graph_graph rejects invalid direction values."""

    def test_invalid_direction_returns_error(self):
        """Verify axiom_graph_graph source validates direction before DB access."""
        query_path = Path(__file__).parent.parent / "axiom_graph" / "query" / "mcp_tools.py"
        source = query_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        # Find the axiom_graph_graph function
        graph_func = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "axiom_graph_graph":
                graph_func = node
                break
        assert graph_func is not None, "axiom_graph_graph function not found"

        # Check that there's a direction validation check (comparing against valid_directions)
        source_text = ast.get_source_segment(source, graph_func)
        assert "valid_directions" in source_text, (
            "Expected direction validation using 'valid_directions' set in axiom_graph_graph"
        )
        assert '"out"' in source_text and '"in"' in source_text and '"both"' in source_text, (
            "Expected 'out', 'in', 'both' in direction validation"
        )


class TestHeaderFormat:
    """US-4: Response headers use consistent [N of M X] format."""

    def test_axiom_graph_graph_always_has_header(self):
        """Verify axiom_graph_graph source always emits a header (not only on truncation)."""
        query_path = Path(__file__).parent.parent / "axiom_graph" / "query" / "mcp_tools.py"
        source = query_path.read_text(encoding="utf-8")
        # The old code only had header inside "if truncated:" block
        # The new code has an unconditional header assignment
        # Check that the return always includes a header with "edges]"
        assert '"edges]"' in source or "edges]" in source, "Expected 'edges]' format string in axiom_graph_graph"
        # Verify the header is NOT only inside an if-truncated block
        # by checking there's a header variable assigned unconditionally
        tree = ast.parse(source)
        graph_func = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "axiom_graph_graph":
                graph_func = node
                break
        assert graph_func is not None
        func_source = ast.get_source_segment(source, graph_func)
        # Should have an unconditional header line that always includes "edges]"
        # Post cycle-3 the api returns shown/total_edges on a GraphResult dataclass
        # and the wire wrapper interpolates them; either literal form is acceptable.
        ok = (
            'f"[{shown} of {total_edges} edges]"' in func_source
            or 'f"[{result.shown} of {result.total_edges} edges]"' in func_source
        )
        assert ok, "Expected unconditional header with [N of M edges] format"

    def test_axiom_graph_sql_has_header(self):
        """Verify ``run_sql`` (api layer post-cycle-3) emits [N of M rows] header."""
        api_path = Path(__file__).parent.parent / "axiom_graph" / "query" / "api.py"
        source = api_path.read_text(encoding="utf-8")
        assert "rows]" in source, "Expected 'rows]' format in query.api.run_sql"

    def test_axiom_graph_history_bracket_format(self):
        """Verify axiom_graph_history uses [N of M entries] not parenthesized format."""
        lc_path = Path(__file__).parent.parent / "axiom_graph" / "lifecycle" / "mcp_tools.py"
        source = lc_path.read_text(encoding="utf-8")
        assert "entries]" in source, "Expected 'entries]' format in axiom_graph_history"
        # Should NOT have the old parenthesized format
        assert "(showing" not in source, "Old '(showing N of M)' format should be replaced with bracket format"


class TestParamRenameCompat:
    """US-3: Parameter rename backward compatibility."""

    def test_axiom_graph_history_has_max_results_param(self):
        """Verify axiom_graph_history uses max_results (not limit) as primary parameter."""
        lc_path = Path(__file__).parent.parent / "axiom_graph" / "lifecycle" / "mcp_tools.py"
        source = lc_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        # Find axiom_graph_history function
        history_func = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "axiom_graph_history":
                history_func = node
                break
        assert history_func is not None, "axiom_graph_history function not found"

        # Check parameter names
        param_names = [arg.arg for arg in history_func.args.args]
        assert "max_results" in param_names, "Expected 'max_results' parameter in axiom_graph_history"

    def test_axiom_graph_history_keeps_limit_alias(self):
        """Verify axiom_graph_history still accepts 'limit' as a deprecated alias."""
        lc_path = Path(__file__).parent.parent / "axiom_graph" / "lifecycle" / "mcp_tools.py"
        source = lc_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        # Find axiom_graph_history function
        history_func = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "axiom_graph_history":
                history_func = node
                break
        assert history_func is not None

        param_names = [arg.arg for arg in history_func.args.args]
        assert "limit" in param_names, "Expected 'limit' parameter as deprecated alias in axiom_graph_history"

    def test_axiom_graph_sql_uses_max_results(self):
        """Verify axiom_graph_sql uses max_results (not max_rows) as primary parameter."""
        query_path = Path(__file__).parent.parent / "axiom_graph" / "query" / "mcp_tools.py"
        source = query_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        sql_func = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "axiom_graph_sql":
                sql_func = node
                break
        assert sql_func is not None

        param_names = [arg.arg for arg in sql_func.args.args]
        assert "max_results" in param_names, "Expected 'max_results' parameter in axiom_graph_sql"


class TestDBLockTiming:
    """Layer 1/5: DB lock timing."""

    def test_connect_logs_slow_lock(self, tmp_path):
        """Verify _connect logs WARNING when lock acquisition is slow."""
        from axiom_graph.index import db as db_mod

        db_path = tmp_path / "test.db"
        db_mod.init_db(db_path)

        _logger = logging.getLogger("axiom_graph.index.db")
        real_connect = db_mod.sqlite3.connect
        call_count = [0]

        def slow_connect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # Simulate slow connect by manipulating monotonic
                pass
            return real_connect(*args, **kwargs)

        # Instead of patching monotonic, patch connect to add delay
        real_monotonic = time.monotonic

        mono_calls = [0]

        def fake_monotonic():
            mono_calls[0] += 1
            if mono_calls[0] % 2 == 1:
                return 0.0  # before connect
            return 0.2  # after connect: 200ms

        with (
            patch.object(db_mod.time, "monotonic", side_effect=fake_monotonic),
            patch.object(_logger, "warning") as mock_warn,
        ):
            with db_mod._connect(db_path) as conn:
                pass
            assert mock_warn.call_count >= 1
            warn_text = str(mock_warn.call_args)
            assert "slow" in warn_text.lower() or "connect" in warn_text.lower()
