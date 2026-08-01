# API MVP (черновик)

Базовый prefix: `/api/v1`  
Auth: `Authorization: Bearer <jwt>`

## Auth

| Method | Path | Описание |
|--------|------|----------|
| POST | `/auth/login` | `{email, password}` → `{access_token, role}` |
| GET | `/auth/me` | текущий пользователь |

## Projects

| Method | Path | Роли | Описание |
|--------|------|------|----------|
| GET | `/projects` | any | список |
| POST | `/projects` | admin | создать + upload PDF (multipart) |
| GET | `/projects/{id}` | any | детали + счётчики статусов |
| POST | `/projects/{id}/pipeline` | admin | запустить extract→ocr→llm |
| POST | `/projects/{id}/export` | admin, scholar | HTML/PDF в storage |
| GET | `/projects/{id}/export/{fmt}` | any if published | скачать |

## Pages

| Method | Path | Роли | Описание |
|--------|------|------|----------|
| GET | `/projects/{id}/pages` | any | список (фильтр `?status=`) |
| GET | `/pages/{id}` | any | html + meta + scan url |
| GET | `/pages/{id}/scan` | any | PNG |
| PATCH | `/pages/{id}` | expert | `{html}` сохранить; пишет version |
| POST | `/pages/{id}/submit-expert` | expert | status → `expert_done` |
| POST | `/pages/{id}/open-scholar` | scholar | status → `scholar_review` |
| POST | `/pages/{id}/publish` | scholar | status → `published` |
| POST | `/pages/{id}/reopen` | admin | откат статуса |
| POST | `/pages/{id}/rerun-llm` | expert, admin | очередь LLM |
| GET | `/pages/{id}/versions` | any | история |
| POST | `/pages/{id}/comments` | expert, scholar | комментарий |

## Scholar assistant

| Method | Path | Роли | Описание |
|--------|------|------|----------|
| POST | `/pages/{id}/assistant` | scholar | `{directive}` → turn + proposed_html |
| POST | `/pages/{id}/assistant/{turn_id}/accept` | scholar | применить → version `scholar` |
| POST | `/pages/{id}/assistant/{turn_id}/reject` | scholar | отклонить |

Пример `POST /assistant`:

```json
{
  "directive": "В примере 3 замени अयम् на एषः — указательное местоимение м.р."
}
```

Ответ:

```json
{
  "turn_id": "...",
  "diff_summary": "अयम् → एषः in shloka 3",
  "proposed_html": "<p class=\"shloka\">...</p>",
  "model": "gpt-4o-mini"
}
```

## Jobs

| Method | Path | Описание |
|--------|------|----------|
| GET | `/jobs/{id}` | статус Celery-задачи |
| GET | `/projects/{id}/jobs` | очередь проекта |

---

## Ошибки

Стандарт: `{ "detail": "..." }` + HTTP 4xx/5xx.  
LLM-quota / 402 → `503` с кодом `llm_quota`.
