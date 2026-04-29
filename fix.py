import sys

with open('apps/bot/handlers/user/my_configs.py', 'r', encoding='utf-8') as f:
    content = f.read()

target = """        except Exception as exc:
            logger.warning("Failed to build vless_uri for sub %s: %s", sub.id, exc)"""

replacement = """        except Exception as exc:
            logger.warning("Failed to build vless_uri for sub %s: %s", sub.id, exc)
    else:
        from models.ready_config import ReadyConfigItem
        ready_item = await session.scalar(select(ReadyConfigItem).where(ReadyConfigItem.subscription_id == sub.id))
        if ready_item:
            vless_uri = ready_item.content"""

if target in content:
    with open('apps/bot/handlers/user/my_configs.py', 'w', encoding='utf-8') as f:
        f.write(content.replace(target, replacement))
    print("SUCCESS")
else:
    print("NOT FOUND")
