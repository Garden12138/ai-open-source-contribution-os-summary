"""Bounded, data-only dependency failures; raw package output never leaves Runner."""
from __future__ import annotations

import re
from app.security import ensure_no_sensitive_data

PHASES = {"node_tools", "node_runtime", "node_resolve", "node_build", "node_native", "create_venv", "build_tools_resolve", "build_tools_install", "project_metadata",
          "runtime_resolve", "runtime_install", "project_build", "project_install", "dependency_check", "upstream_source_build", "source_acquire", "baseline_verify"}
REASONS = {"toolchain_version_unavailable", "package_tls_failed", "package_integrity_failed", "package_write_denied", "memory_exhausted", "dependency_integrity_changed", "no_matching_distribution", "dependency_conflict", "proxy_denied", "network_unavailable",
           "disk_exhausted", "subprocess_failed", "build_tool_unavailable", "native_tool_unavailable", "source_distribution_missing", "source_hash_mismatch", "source_archive_invalid", "source_metadata_invalid"}


def summarize_dependency_failure(phase: str, exit_code: int, output: str) -> dict:
    """Self-contained so the exact classifier can run inside the trusted script."""
    import re
    output = output[-65536:]
    lower = output.lower()
    reason = "subprocess_failed"
    if "no matching toolchain version" in lower or "no matching runtime version" in lower:
        reason = "toolchain_version_unavailable"
    elif "no space left on device" in lower:
        reason = "disk_exhausted"
    elif any(v in lower for v in ("enomem", "heap out of memory", "allocation failed")):
        reason = "memory_exhausted"
    elif any(v in lower for v in ("self_signed_cert_in_chain", "unable_to_verify_leaf_signature", "cert_has_expired")):
        reason = "package_tls_failed"
    elif "eintegrity" in lower or "integrity checksum failed" in lower:
        reason = "package_integrity_failed"
    elif "eacces" in lower or "eperm" in lower:
        reason = "package_write_denied"
    elif phase == "baseline_verify" and "pinneddownloaderror" in lower and "ffmpeg" in lower:
        reason = "native_tool_unavailable"
    elif any(v in lower for v in ("403 forbidden", "tunnel connection failed: 403")):
        reason = "proxy_denied"
    elif any(v in lower for v in ("connectionerror", "proxyerror", "sslerror", "readtimeout", "connecttimeout", "temporary failure in name resolution", "eai_again", "enotfound", "econnreset", "etimedout", "econnrefused")):
        reason = "network_unavailable"
    elif "resolutionimpossible" in lower or "conflicting dependencies" in lower:
        reason = "dependency_conflict"
    elif "no matching distribution found" in lower or "could not find a version that satisfies" in lower:
        reason = "no_matching_distribution"
    elif phase in {"project_build", "project_metadata", "upstream_source_build"} and "uv-build was not properly installed" in lower:
        reason = "build_tool_unavailable"
    packages = sorted(set(re.findall(
        r"(?:No matching distribution found for |Could not find a version that satisfies the requirement )([A-Za-z0-9][A-Za-z0-9_.-]{0,99})(?=[\s<>=!~;\[(:]|$)", output)))[:8]
    if reason == "build_tool_unavailable":
        packages = ["uv-build"]
    elif reason == "native_tool_unavailable":
        packages = ["ffmpeg", "ffprobe"]
    return {"version":"dependency-failure-v1", "phase":phase,
            "reason_code":reason, "exit_code":exit_code, "packages":packages}


def validate_dependency_diagnostic(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != {"version","phase","reason_code","exit_code","packages"}:
        raise ValueError("Invalid dependency diagnostic")
    if (value["version"] != "dependency-failure-v1" or value["phase"] not in PHASES
            or value["reason_code"] not in REASONS or type(value["exit_code"]) is not int
            or not -255 <= value["exit_code"] <= 255 or not isinstance(value["packages"], list)
            or len(value["packages"]) > 8 or any(not isinstance(p,str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}",p) for p in value["packages"])):
        raise ValueError("Invalid dependency diagnostic")
    ensure_no_sensitive_data(value, context="dependency diagnostic")
    return value


def dependency_diagnostic_message(value: dict) -> str:
    value = validate_dependency_diagnostic(value)
    phases = {"node_native":"原生工作区平台包准备", "node_tools":"Node 包管理器准备", "node_runtime":"Node 版本选择", "node_resolve":"Node 工作区依赖解析", "node_build":"Node 依赖离线构建", "baseline_verify":"修改前离线测试", "source_acquire":"依赖源码获取", "upstream_source_build":"上游依赖离线编译", "build_tools_resolve":"构建工具 wheel 解析", "runtime_resolve":"运行和测试依赖 wheel 解析",
              "project_metadata":"项目元数据构建", "create_venv":"隔离环境创建",
              "build_tools_install":"构建工具安装", "runtime_install":"冻结依赖安装",
              "project_build":"项目 wheel 构建", "project_install":"项目 wheel 安装", "dependency_check":"依赖一致性检查"}
    reasons = {"toolchain_version_unavailable":"允许的包来源中没有满足项目声明的工具链版本", "package_tls_failed":"包服务 TLS 证书校验未通过", "package_integrity_failed":"下载包的完整性校验未通过",
               "package_write_denied":"包管理器无法写入隔离的安装或缓存目录", "memory_exhausted":"沙箱内存预算耗尽",
               "dependency_integrity_changed":"冻结依赖清单在检查前后不一致，未采纳本次测试结果；需要核对隔离环境和工作区链接",
               "source_distribution_missing":"没有符合约束的源码包", "source_hash_mismatch":"源码哈希不匹配", "source_archive_invalid":"源码归档结构不安全", "source_metadata_invalid":"源码构建声明无效", "no_matching_distribution":"没有匹配当前 Python/平台及版本约束的 wheel",
               "dependency_conflict":"依赖版本约束冲突", "proxy_denied":"包代理拒绝访问",
               "network_unavailable":"包代理连接或上游网络未完成", "disk_exhausted":"沙箱磁盘预算耗尽",
               "subprocess_failed":"子进程未成功完成", "build_tool_unavailable":"隔离环境未能找到已安装的构建工具，需要修复准备器的工具路径",
               "native_tool_unavailable":"项目固定的原生测试工具尚未准备，断网测试不能临时下载；需要独立的固定资源准备步骤"}
    packages = "（" + "、".join(value["packages"]) + "）" if value["packages"] else ""
    return phases[value["phase"]] + "失败：" + reasons[value["reason_code"]] + packages + "；尚未修改代码。"
