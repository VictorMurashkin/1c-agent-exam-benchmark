"""Public exam guidance, distinct from the statement and private test cases."""
import json
from pathlib import Path
from .common import digest, tree_manifest

INPUT_REVISION = 'open-methodics-v5'
METHODS_REF = 'public/methodical-requirements.txt'
POLICY_REF = 'public/assessment-policy.md'
SOURCE_SHA256 = '9cfbc21905fd378274f2d8e0f8a428ac0c83733c8f0a48e08d1fc4c7215fcc61'
SOURCE_TEXT_SHA256 = 'b9c2d83d4010fbcef45e3e3885584ad8bce1d441b7a9995e9af5bc99d226f216'

DEFAULT_PROMPT = (
    'Решите экзаменационное задание task/statement.docx. Начните с чтения '
    'task/statement.txt, task/methodical-requirements.txt, task/assessment-policy.md, '
    'task/protocol.md и worker.json; рисунки находятся в task/assets/. '
    'Соблюдайте открытые методические требования 1С и явно указанные отличия правил стенда. '
    'Применяйте требования к механизмам только там, где они относятся к заданию. '
    'При пакетных запусках конфигуратора (DESIGNER) в Windows добавляйте '
    '/DisableStartupDialogs /DisableStartupMessages и не добавляйте /Visible: '
    'это служебные операции без показа окон; Start-Process -WindowStyle Hidden '
    'не заменяет эти параметры платформы. '
    'Сдайте результат в out/ по протоколу, включая out/interpretations.json со спорными местами условия '
    'и вашей трактовкой каждого. Дополнительных разрешений не запрашивайте; '
    'если чего-то не хватает, зафиксируйте ограничение и оставьте исходники.'
)
# Owner decision 2026-10-13: a solver without a clock misjudged elapsed time and stopped after 4.5 of 30 minutes.
# Plans with deadline_disclosed give the absolute end of the limit in worker.json and this line in the prompt.
DEADLINE_PROMPT = (
    ' Лимит времени истекает в момент deadline_utc из worker.json; текущее время проверяйте командой date -u. '
    'Не завершайте работу досрочно, пока остаются нерешённые разделы: используйте всё время, '
    'но держите out/ в состоянии, пригодном к сдаче, и подайте сигнал готовности до срока.'
)


def solver_prompt(plan, prompt=None):
    """The prompt a solve starts with: an explicit one (probes) or the default exam prompt, plus the deadline line."""
    if prompt is not None:
        return prompt
    return DEFAULT_PROMPT + (DEADLINE_PROMPT if plan.get('deadline_disclosed') else '')


def extract_methods(source):
    """Keep exactly the official preface, stopping before any sample task."""
    lines = source.splitlines(keepends=True)
    boundaries = [i for i, line in enumerate(lines) if line.strip() == 'Примеры заданий:']
    if boundaries != [278]:
        raise ValueError('Unexpected official preface boundary')
    return ''.join(lines[:278])


def render_methods(docx, source):
    """The official preface from the Word document, tables drawn with ASCII borders.

    The words must be exactly those of the official text preface (as a multiset): only the layout changes."""
    from collections import Counter
    from . import docx_text
    elements, _ = docx_text.body(docx)
    boundary = [i for i, e in enumerate(elements) if docx_text.text_of(e).strip() == 'Примеры заданий:']
    if len(boundary) != 1:
        raise ValueError('Unexpected official preface boundary in the Word document')
    text = '\n'.join(docx_text.render(elements[:boundary[0]])) + '\n'
    if Counter(docx_text.words(text)) != Counter(docx_text.words(extract_methods(source))):
        raise ValueError('Rendered methodical requirements differ from the official text')
    return text


def render_policy(policy, execution):
    """Render only disclosed decisions; never serialize the private policy wholesale."""
    if (policy['range_selection'] != 'minimum'
            or policy['overlap']['different_penalty_ids_for_same_defect'] != 'smallest_applicable'
            or policy['overlap']['repeated_observations_of_same_pair'] != 'one_penalty_instance'
            or policy['additional_document_types']['waived_penalty_ids'] != []
            or policy['shared_invoice_types']['duplicate_types_between_sections'] != 'forbidden'
            or policy['recalculation']['current_task_mode'] != 'prohibited'
            or policy['execution_budget']['series'] != {k: v['solve_timeout_seconds'] for k, v in execution['series'].items()}
            or policy['bonus_policy'] != 'not_awarded' or policy['score_clamping'] != 'none'):
        raise ValueError('Public policy renderer needs updating for the owner decisions')
    labels = {'unsatisfactory': 'неудовлетворительно', 'satisfactory': 'удовлетворительно',
              'good': 'хорошо', 'excellent': 'отлично'}
    intervals = []
    for interval in policy['grade_intervals']:
        lo, hi = interval['lower'], interval['upper']
        expression = 'балл'
        if lo is not None:
            expression = lo + (' ≤ ' if interval['lower_inclusive'] else ' < ') + expression
        if hi is not None:
            expression += (' ≤ ' if interval['upper_inclusive'] else ' < ') + hi
        intervals.append(f"- {expression}: {labels[interval['grade']]}.")
    series = execution['series']
    return f'''# Открытые правила оценки стенда — {INPUT_REVISION}

Это правила владельца benchmark, а не новая редакция официальной методики 1С.
Официальная вводная часть и полный опубликованный список ошибок со штрафами находятся
в `task/methodical-requirements.txt`. Чистое условие — в `task/statement.docx`
и `task/statement.txt`. Решение и книга с разбором в выдачу не входят.

Источник: [официальные материалы экзамена 1С](https://1c.ru/spec/questions.htm),
документ `ATT83PL.rtf`, полученный 9 октября 2026 года. SHA-256 исходного документа:
`{SOURCE_SHA256}`. В методику включены только строки 1–278 текстовой копии,
до раздела «Примеры заданий:»; официальный текст сохранён без исправлений.

Стенд применяет официальную методику как есть. Ниже — только то, чего методика
не регулирует, и единственное отличие условий (время и материалы).

- Начальный балл: {policy['initial_points']}. Штраф с диапазоном начисляется по нижней границе диапазона.
  Пока проверены не все пункты перечня, балл стенда — верхняя граница: непроверенные пункты
  могут его только понизить. Несдача доказана, когда эта граница ниже 2.2.
- Один дефект — один штраф: если ошибка подпадает под несколько пунктов перечня,
  начисляется наименьший из них. Повторное наблюдение того же дефекта
  не создаёт дополнительного штрафа. Число тестов не является множителем.
- Собственный вид документа, не описанный в задании, штрафуется по официальному перечню.
  Приходная и расходная накладные общие для разделов учёта: отдельные типы накладных
  для оперативного и бухгалтерского разделов штрафуются по официальному перечню.
- В текущем билете механизм перерасчётов использовать не надо, как сказано в условии.
  Задачи по бизнес-процессу в этом билете нет: штрафы раздела бизнес-процессов не применяются.
- Сдача должна быть построена на выданном каркасе (тот же идентификатор конфигурации);
  изменять структуру каркаса можно. Конфигурация, которая не загружается в новую базу из DT каркаса,
  означает несдачу: требуется действующий вариант конфигурации.
- Демонстрационные данные обязательны: объявите процедуру их заполнения в `out/binding.json`
  (см. `task/protocol.md`). Без неё или при её ошибке применяется официальный штраф за тестовые данные.
- Доказанное отсутствие решения обязательного раздела оперативного учёта,
  бухгалтерского учёта или периодических расчётов означает несдачу.
  Неподдержанная привязка и сбой проверяющего не доказывают отсутствие решения:
  при невозможности независимой проверки раздел остаётся не аттестованным.
- Время: основная серия — {series['exam']['solve_timeout_seconds'] // 3600} часов, как у человека на экзамене;
  быстрая серия — {series['fast']['solve_timeout_seconds'] // 60} минут. Лимит этого запуска указан в `worker.json`.
  Подготовка среды и независимая приёмка измеряются отдельно. По таймауту агент
  останавливается, доступная сдача замораживается; автоматического повтора нет.
- Материалы: режим сети этого запуска указан в `worker.json`. Дополнительный харнес
  (если есть) — часть испытуемого стека, его состав тоже в `worker.json`.
  Штатный веб-поиск и нативные решающие субагенты отключены.
- Приёмка использует формальные проверки исходников и сценарии в настоящей 1С.
  ИИ не выносит оценку. Открытый каталог требований не означает, что все проверки
  уже реализованы: отчёт показывает покрытие, неизвестные пункты и ошибки проверяющего.
  Скрытые тестовые данные, ожидаемые результаты и код проверяющего агенту не выдаются.

Границы оценок — как в документе 1С (нижняя граница входит в интервал):

{chr(10).join(intervals)}

Бонусы за оригинальность и эффективность не начисляются: правила начисления в методике нет.
Балл снизу не ограничивается. Неэффективность определяется по признакам в коде, а не по замерам
времени. Собеседование на стенде не проводится, штраф за дополнительный вопрос не применяется.

Версии решений владельца: `{policy['policy_id']}`, `{execution['policy_id']}`.
Это открытые правила для подготовки решения, без скрытых сценариев и подсказок к реализации.
'''


def delivery_metadata(task):
    manifest = json.loads((Path(task) / 'manifest.json').read_text(encoding='utf-8'))
    public = manifest['public']
    if public.get('input_revision') != INPUT_REVISION:
        raise ValueError('Open exam materials revision missing or unsupported')
    if (public.get('methodical_requirements_ref') != METHODS_REF
            or public.get('assessment_policy_ref') != POLICY_REF):
        raise ValueError('Open exam materials references differ')
    hashes = public['guidance_sha256']
    if set(hashes) != {METHODS_REF, POLICY_REF}:
        raise ValueError('Unexpected open exam materials inventory')
    for ref, expected in hashes.items():
        if digest(Path(task) / ref) != expected:
            raise ValueError('Open exam materials hash mismatch: ' + ref)
    return {'input_revision': INPUT_REVISION, 'methodical_requirements_disclosed': True}


def verify_delivery_plan(plan, task):
    metadata = delivery_metadata(task)
    if any(plan.get(k) != v for k, v in metadata.items()):
        raise ValueError('Plan does not pin the open exam materials revision; create a new plan')
    if plan.get('public_inputs') != tree_manifest(Path(task) / 'public'):
        raise ValueError('Public issue package differs from pinned plan')
    return metadata
