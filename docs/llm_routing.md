# LLM routing — результаты Infant Reader (эмпирика)

Источник: обработка `sources/Infant-Reader.pdf` (101 стр.) через ProxyAPI.ru.  
Цель: рекомендуемые конфиги для `sanskrit_srv` workers.

---

## Краткий вердикт

| Тип страницы | Что сработало | Что ломалось |
|--------------|---------------|--------------|
| **Уроки / проза / стихи** (≈17–101) | **gemini-3.5-flash** — основной проход | Иногда «думает вслух»; длинные таблицы обрезает |
| **Длинные таблицы** (TOC, словари) | **gemini-2.5-flash** (retry) | 3.5-flash обрезает на `style="` |
| **Алфавит / сетки akshara** (6–16) | **gpt-4o-mini** (полнота HTML) | Gemini: обрезка / reasoning |
| **Плотные conjunct grids** (15+) | **ручная правка** или сверка экспертом | **gpt-4o** тоже путает ячейки |
| **Титул** | ручной seed | — |

**Ни одна модель не «не напутала» на алфавитных сетках на 100%.**  
Непутая зона = **уроки без плотных таблиц** на **Gemini Flash**.  
Для сеток: OpenAI даёт *полный* HTML чаще, но **содержание ячеек** нужно валидировать / править экспертом.

---

## Рейтинг по задачам

### 1. gemini-3.5-flash (ProxyAPI `/google`)
- **Сильные:** обычные уроки (shloka, 2–3 колонки предложений), intro, questions
- **Слабые:** длинный output → truncation; алфавит; «Let's look at the image…»
- **Роль в сервисе:** `provider=gemini` default для `page_type=lesson|prose`

### 2. gemini-2.5-flash
- **Сильные:** retry при truncation / reasoning у 3.5; стабильнее на длинных таблицах
- **Слабые:** те же алфавитные сетки
- **Роль:** fallback #1 внутри Gemini family

### 3. gemini-3-flash-preview
- **Сильные:** иногда спасал отдельные страницы (напр. 18)
- **Слабые:** алфавит 6/11/14 — fail
- **Роль:** fallback #2

### 4. gpt-4o-mini (ProxyAPI `/openai/v1`)
- **Сильные:** стр. **6, 11, 14** — полные таблицы где Gemini fail; лучше слушается «только HTML»
- **Слабые:** стр. **7, 15** — полный HTML, но **неверные ряды/клетки** (перестановка, hallucinated last row)
- **Роль:** `page_type=alphabet|grid` primary

### 5. gpt-4o
- **Сильные:** быстрее отдаёт полный каркас таблицы
- **Слабые:** стр. **15** — много ошибочных conjuncts (श्म вместо ष्म, лишние строки, кривой заголовок)
- **Роль:** optional upgrade; не гарантия точности IAST/Devanagari

### 6. Manual / template
- **Сильные:** стр. **1, 7, 15** — ground truth
- **Роль:** `page_type=title` seed; `alphabet` после LLM → expert queue automatically

---

## Рекомендуемый routing (для конфига сервиса)

```yaml
# sanskrit_srv / project.settings.llm
llm:
  proxyapi:
    api_key_env: OPENAI_API_KEY          # один ключ ProxyAPI
    gemini_base_url: https://api.proxyapi.ru/google
    openai_base_url: https://api.proxyapi.ru/openai/v1

  routes:
    title:
      mode: seed_or_manual               # без LLM или раз
    alphabet:                            # akshara / vowels / conjuncts
      primary: { provider: openai, model: gpt-4o-mini }
      fallback: { provider: openai, model: gpt-4o }
      on_fail: queue_expert              # не OCR-мусор
      max_output_tokens: 8192
      temperature: 0
      prompt: alphabet                   # ALPHABET_PROMPT
      auto_status: expert_review         # ВСЕГДА на эксперта
    toc:
      primary: { provider: gemini, model: gemini-2.5-flash }
      fallback: { provider: gemini, model: gemini-3.5-flash }
      max_output_tokens: 16384
    lesson:
      primary: { provider: gemini, model: gemini-3.5-flash }
      fallback:
        - { provider: gemini, model: gemini-2.5-flash }
        - { provider: openai, model: gpt-4o-mini }
      max_output_tokens: 8192
      temperature: 0
      prompt: lesson                     # SYSTEM_PROMPT

  validation:
    reject_if:
      - garbage_reasoning                # "Let's look", "Wait, the"
      - truncated_html                   # unclosed table/tr/td
      - no_devanagari_on_sa_page
    on_reject: try_next_route_then_expert

  cost_hint_usd_per_100_pages: "1–5"
```

---

## Эвристика `page_type` (авто)

| Признаки | type |
|----------|------|
| Ранние стр. + «अक्षरमाला / स्वराः / व्यञ्जन / संयुक्त» | `alphabet` |
| «अनुक्रमणिका / CONTENTS» | `toc` |
| «पाठः» / shloka / questions | `lesson` |
| Титул / publisher block | `title` |

Infant Reader mapping: стр. PDF **6–16 ≈ alphabet**, **4–5 ≈ toc**, **17+ ≈ lesson**.

---

## Честный ответ на «кто НЕ напутал»

1. **Для уроков (основной объём книги):** ближе всего к «не напутал» — **gemini-3.5-flash** (+ retry 2.5-flash). Туда и уходит 80%+ страниц.
2. **Для полных HTML-сеток:** лучше **gpt-4o-mini**, но это «не обрезал», а не «не напутал в буквах».
3. **Для точных лигатур (conjuncts):** ни Gemini, ни GPT — **эксперт / ручной seed**; LLM только черновик.

Итог для продукта: **routing по типу страницы + обязательный expert_review на alphabet**, не одна «лучшая» модель.

---

## Связь с кодом пайплайна

Уже реализовано в `infant_reader_ocr/07_llm_verify.py`:

- `--provider auto|gemini|openai`
- `ALPHABET_PAGES` → `ALPHABET_PROMPT`
- fallback models lists
- `html_utils.is_garbage_html` / truncation

При портации в Celery workers — перенести эту матрицу в `projects.settings` (JSONB), не хардкодить в коде.
