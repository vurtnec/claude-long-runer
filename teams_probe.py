"""
Teams 探测/初始化脚本
====================

两个用途：
1. **首次登录**：在 scheduler 启动前完成 OAuth device code flow，token 缓存到
   ~/.claude-long-runner/teams_token_cache.json。之后 daemon 静默刷新即可。
2. **可视化验证**：列出最近 chat + 每个 chat 的最近几条消息，确认 Graph API
   能拉到 Teams 桌面 app 看到的同一份数据。

实现：复用 ``scheduler/teams_client.py``，避免重复 OAuth/HTTP 逻辑。

用法：
    pip install msal requests
    python teams_probe.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# 让脚本在没有把项目装成 package 的情况下直接 import scheduler.*
sys.path.insert(0, str(Path(__file__).parent))

try:
    from scheduler.teams_client import TeamsAuthError, get_teams_client
except ImportError as e:
    print(f"导入失败 ({e})。请先 pip install msal requests")
    sys.exit(1)


def main() -> None:
    client = get_teams_client()

    print("== 第 1 步：登录（首次会弹 device code，之后用缓存） ==")
    try:
        token = client.get_access_token(interactive=True)
    except TeamsAuthError as e:
        print(f"❌ {e}")
        sys.exit(1)
    print(f"✅ access_token 长度 {len(token)}")

    print("\n== 第 2 步：验证身份 ==")
    try:
        me = client._get("/me")
    except Exception as e:
        print(f"❌ /me 调用失败: {e}")
        sys.exit(1)
    print(f"   账号: {me.get('displayName')} <{me.get('userPrincipalName')}>")

    print("\n== 第 3 步：列出最近 5 个 chat ==")
    try:
        chats = client.list_chats(top=5)
    except Exception as e:
        print(f"❌ list_chats 失败: {e}")
        sys.exit(1)
    if not chats:
        print("   ⚠️  返回空，账号可能没有 Teams license / 还没用过 Teams")
        return
    print(f"   ✅ 找到 {len(chats)} 个 chat\n")

    print("== 第 4 步：读每个 chat 最近 3 条消息 ==")
    for i, chat in enumerate(chats, 1):
        chat_id = chat["id"]
        topic = chat.get("topic") or f"({chat.get('chatType', 'unknown')})"
        last_preview = (chat.get("lastMessagePreview") or {}).get("body", {}).get(
            "content", ""
        )
        last_preview = last_preview.replace("\n", " ")[:80]

        print(f"\n--- [{i}] {topic} ---")
        print(f"    chat_id: {chat_id}")
        if last_preview:
            print(f"    最新预览: {last_preview}")

        try:
            messages = client.get_chat_messages(chat_id, since=None, top=3)
        except Exception as e:
            print(f"    ❌ 拉消息失败: {e}")
            continue
        for msg in messages[-3:]:
            print(
                f"    [{msg.created_at}] {msg.sender_name or '(系统)'}: "
                f"{msg.body_text[:120]}"
            )

    print("\n" + "=" * 60)
    print("✅ 测试完成。如果上面看到了真实消息，登录已生效；")
    print("   现在可以把 schedules/_examples/teams_*.yaml 拷贝到 schedules/，")
    print("   编辑后用 `python -m scheduler.daemon` 启动。")
    print("=" * 60)


if __name__ == "__main__":
    main()
