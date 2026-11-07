# Архив

Эти файлы не удалены. Перед переносом проверено, что их не импортируют `bot.py`, `python -m support_agent.service`, Procfile и Dockerfile помощника.

- `root/` — старые копии `catalog_context.py`, `answer_check.py`, `apple_matching.py` в корне. Рабочие копии лежат в `support_agent/`.
- `support_agent/core.py` и `dialog_media.py` — ни один текущий модуль их не вызывает.
- `knowledge.json` — старые цены и описания, в код не подключены.
- Старые обзоры диалогов и `SAFETY_CHECKS.md`. Живой чеклист теперь в `support_agent/DEPLOY.md`.

Не переносились, потому что ещё используются или это не доказано: `part40.md`, функции `claude_with_search` в `common.py` (маршрут из бота уже не вызывает её), продажа и активация Claude, фоновые задачи.
