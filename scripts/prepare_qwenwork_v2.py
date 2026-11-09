"""Generate machine-specific Qwen import files; never install into the host.

Run after local dependencies have been installed. Re-run after moving the app.
No credentials, resumes, recordings or databases are read by this script.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import zipfile


DEFAULT_ROOT = Path(__file__).resolve().parents[1]


def prepare(project_root=DEFAULT_ROOT, output=None, port=8767):
    root = Path(project_root).resolve()
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Invalid local port")
    target = Path(output).resolve() if output is not None else root / "delivery" / "qwenwork-v2"
    # Generated paths are staging resources, not an alternative host installer.
    if not target.is_relative_to(root / "delivery"):
        raise ValueError("Output must be inside this project's delivery directory")
    python = root / ".venv-suit" / "Scripts" / "python.exe"
    entry = root / "src" / "mmsa_v2" / "qwen_stdio.py"
    source = root / "docs" / "qwenwork-v2-staging" / "zhenjian-v2" / "SKILL.md"
    for path in (python, entry, source):
        if not path.is_file():
            raise FileNotFoundError("Run the V2 installer first; missing " + str(path.relative_to(root)))
    config = {"mcpServers": {"zhenjian-v2": {"type": "stdio", "command": str(python),
              "args": ["-X", "utf8", str(entry), "--port", str(port)],
              "env": {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}}}}
    target.mkdir(parents=True, exist_ok=True)
    config_path = target / "mcp-import.local.json"
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    skill = target / "zhenjian-v2"
    (skill / "references").mkdir(parents=True, exist_ok=True)
    content = source.read_text(encoding="utf-8")
    (skill / "SKILL.md").write_text(content, encoding="utf-8")
    local = f"""# 本机安装位置

项目目录：`{root}`

本机 Python：`{python}`

连接器导入文件：`{config_path}`

默认服务：`http://127.0.0.1:{port}/v2/`，仅限本机。

完整初始化：读取项目根目录 QWEN_HANDOFF.md，运行 scripts/first-run-v2.ps1。它负责环境、自检、本机密钥窗口及打开 HR，不依赖先安装本 Skill。
用户自己的百炼账户承担费用，评分固定 qwen3.7-plus，云转写固定 qwen3-asr-flash。
宿主提供连接器管理能力时可按本配置自动添加并启用，再实际调用 `v2_status` 验证。
不具备该能力时，先交付 HR 页面即可；生成文件并不代表宿主已经安装。
若沙箱或宿主拒绝执行初始化脚本，请用户双击“开始使用真才实鉴.cmd”；不得绕过权限或直接写入宿主活跃技能目录。
移动项目目录后重新运行 `scripts/prepare_qwenwork_v2.py`，由用户更新千问连接器配置。
本交付包不包含开发者的30份简历、历史面试、模型账户密钥或本机登录凭据。
"""
    (skill / "references" / "local-install.md").write_text(local, encoding="utf-8")
    archive = target / "zhenjian-v2.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as packed:
        for relative in ("SKILL.md", "references/local-install.md"):
            packed.write(skill / relative, "zhenjian-v2/" + relative)
    guide = """真才实鉴 V2 · 千问本机接入

首次使用让千问读取项目根目录 QWEN_HANDOFF.md，运行统一初始化入口，按提示在本机输入自己的百炼 Key，即可请求打开 HR 总控。
若千问无法执行主机脚本，双击项目根目录“开始使用真才实鉴.cmd”，无需先导入本目录内的文件。
评分固定 qwen3.7-plus，云转写固定 qwen3-asr-flash，费用由使用者自己的百炼账户承担。

本目录用于可选的千问工具接入：宿主提供连接器管理工具时可自动读取 mcp-import.local.json 并添加；Skill可按宿主支持的流程安装。
需要手动接入时：技能页面选择 zhenjian-v2.zip，连接器页面导入 mcp-import.local.json 并启用。
接入完成后实际调用 v2_status，再调用 v2_open_workbench；未接入不影响本机 HR 页面。
总控导入自己的 DOCX 简历并创建面试。学生在安装程序的同一台电脑上作答；老师在总控查看报告与排名。

日常不必手动启动 Python；连接器首次工具调用会启动或复用本机服务。
浏览器打开工具返回的是“已请求打开”，不是已确认用户看到。看不到时双击项目根目录的启动面试工作台.cmd。
Skill 与连接器都需按客户端提供的方式导入；本生成器没有替用户完成宿主安装。
首次下载需要网络；后续本地面部/声音分析不上传原片。百炼评分与云转写仍需要网络和自己的 API 配置。
源码更新不删除已有面试数据。若提示 update_available，等任务结束后用本机启动器更新服务。
"""
    (target / "使用说明.txt").write_text(guide, encoding="utf-8")
    result = {"schema": "zhenjian-qwenwork-local.v2", "connector": "zhenjian-v2", "port": port,
              "project_root": str(root), "config_file": str(config_path), "skill_zip": str(archive),
              "host_installed": False, "contains_credentials": False,
              "skill_sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}
    (target / "manifest.local.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--port", type=int, default=8767)
    args = parser.parse_args(argv)
    try:
        result = prepare(args.project_root, args.output, args.port)
    except (OSError, ValueError) as exc:
        parser.exit(1, "生成失败：" + str(exc) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
