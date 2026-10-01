"""The survey-form runtime, ported from geodit-ui (pure Python, no Qt).

The QGIS feature form is a port of geodit-ui's map **Data** sheet
(``FeatureFormDialog`` → ``ResponseFormSheet`` → ``FormRenderer``), so the
answers QGIS writes obey exactly the rules the web and the Android app apply.
Each module ports one set of web sources (under
``src/features/project-survey/``) function for function — TypeScript names
become snake_case — and names them in its docstring:

* ``jsnum``      — JavaScript ``Number`` semantics the ported code relies on
* ``model``      — the form payload (``services/form/detail/type.ts``,
  ``types/questions.ts``)
* ``timezone``   — DATETIME clock-time digits (``utils/timezone.ts``)
* ``answers``    — answer values, manual entry, choice labels, decoding a stored
  response (``utils/answers.ts``, ``runtime/manualEntry.ts``,
  ``utils/choice-display.ts``, ``data/services/api.ts``)
* ``rules``      — the rules engine (``utils/evaluateRules.ts``,
  ``utils/author-hidden.ts``)
* ``defaults``   — MANUAL / CALCULATE / SHAPEFILE defaults and unique ids
  (``utils/evaluateDefaultValue.ts``, ``schema-parts.ts``,
  ``manual-default-store.ts``, ``computeUniqueId.ts``)
* ``validation`` — per-answer validation (``runtime/form-validation.ts``)
* ``unique``     — the cross-record UNIQUE probe (``runtime/uniqueCheck.ts``,
  ``findAnsUniqueViolations``)
* ``session``    — ``FormRenderer``'s state and submit pipeline, without the UI
* ``media``      — the media-limits contract (``runtime/mediaLimits.ts``,
  ``convert/sniff.ts``, ``convert/keepRules.ts``)
* ``exif``       — carrying capture time and GPS into a re-encoded JPEG
  (``convert/exif.ts``)

Ported from geodit-ui 2b0e16c (2026-09-29). When the web runtime changes, port
the change here too; the unit tests carry the web's own test cases.
"""
