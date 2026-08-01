# Схема данных (MVP)

## Сущности

### users
| Поле | Тип | Описание |
|------|-----|----------|
| id | uuid | PK |
| email | text unique | логин |
| password_hash | text | bcrypt |
| display_name | text | |
| role | enum | `admin`, `expert`, `scholar`, `reader` |
| created_at | timestamptz | |

### projects
| Поле | Тип | Описание |
|------|-----|----------|
| id | uuid | PK |
| slug | text unique | `infant-reader` |
| title | text | Infant Reader |
| title_sa | text | संस्कृतबालबोधः |
| source_pdf_path | text | storage path |
| status | enum | `draft`, `in_progress`, `published` |
| settings | jsonb | LLM model, crop margin, scripts |
| created_by | uuid → users | |
| created_at | timestamptz | |

### pages
| Поле | Тип | Описание |
|------|-----|----------|
| id | uuid | PK |
| project_id | uuid → projects | |
| page_no | int | 1..N |
| scan_path | text | PNG |
| ocr_text | text | raw OCR |
| status | enum | см. ниже |
| current_html | text | актуальный фрагмент |
| assigned_expert_id | uuid? | |
| assigned_scholar_id | uuid? | |
| updated_at | timestamptz | |

**page.status:**  
`pending` → `extracting` → `ocr` → `llm_draft` → `expert_review` → `expert_done` → `scholar_review` → `published`

### page_versions
| Поле | Тип | Описание |
|------|-----|----------|
| id | uuid | PK |
| page_id | uuid → pages | |
| version | int | монотонный |
| html | text | снимок |
| source | enum | `ocr`, `llm`, `expert`, `scholar_assistant`, `scholar` |
| created_by | uuid? | |
| note | text | «LLM gemini-2.5-flash» / директива |
| created_at | timestamptz | |

### comments
| Поле | Тип | Описание |
|------|-----|----------|
| id | uuid | PK |
| page_id | uuid | |
| author_id | uuid | |
| body | text | |
| created_at | timestamptz | |

### assistant_turns
| Поле | Тип | Описание |
|------|-----|----------|
| id | uuid | PK |
| page_id | uuid | |
| scholar_id | uuid | |
| directive | text | естественный язык |
| proposed_html | text | полный фрагмент после правки |
| diff_summary | text | краткое описание |
| status | enum | `pending`, `accepted`, `rejected` |
| model | text | |
| created_at | timestamptz | |
| decided_at | timestamptz? | |

### jobs
| Поле | Тип | Описание |
|------|-----|----------|
| id | uuid | PK |
| project_id | uuid | |
| page_id | uuid? | null = весь проект |
| kind | enum | `extract`, `ocr`, `llm`, `export` |
| state | enum | `queued`, `running`, `done`, `failed` |
| error | text? | |
| celery_id | text? | |
| created_at / finished_at | timestamptz | |

---

## Индексы

- `pages (project_id, page_no)` unique  
- `page_versions (page_id, version)` unique  
- `pages (project_id, status)` для очередей эксперта/учёного  

---

## Seed для демо

Импорт из `../infant_reader_ocr/`:

- `pages/page_*.png` → `storage/.../scans/`
- `llm_verified.LLM_VERIFIED[n]` → `pages.current_html` + `page_versions` (`source=llm`)
- статус: `expert_review` (черновик уже есть)
