# قابلیت جدید: بهینه‌سازی چیدمان استخر (Optimize Pond Allocation)

قابلیتی مستقل کنار شماتیک ۲۱ استخر. معماری فعلی و ماژول سالم
`pond_alloc.py` (تخصیص استخر اول برای cohortهایی که تازه از تراف عبور
می‌کنند) دست‌نخورده ماند.

## فایل‌های جدید

| فایل | نقش |
|---|---|
| `core/pond_optimize.py` | ماژول کاملاً جدید: Preview/Apply/Restore/Forecast |
| `tests/test_pond_optimize.py` | ۱۱ تست، هر ۸ سناریوی الزامی specification |

## فایل‌های تغییرکرده (فقط افزایشی)

- `core/db.py` — جدول append-only جدید `pond_alloc_batches` (audit trail
  برای Restore) + سه متد کمکی
- `config.yaml` — دو Assumption جدید: `pond.max_weight_mix_tolerance_percent`
  (پیش‌فرض ۸٪) و `pond.optimize_horizon_days` (پیش‌فرض ۱۲۰ روز)
- `app.py` — چهار endpoint جدید (`/api/ponds/optimize/preview|apply|restore`,
  `/api/ponds/forecast`)؛ فقط import و چند بلوک `if path==...` اضافه شد
- `static/index.html`, `static/app.js`, `static/styles.css` — دو دکمه،
  Date Selector، رندر Forecast، و پنل Preview (با همان `#drawer`/`#overlay`
  مشترک موجود، بدون المان تکراری)

## معماری

```
optimize_layout()  → Preview فقط — هیچ نوشتنی در DB ندارد
apply_layout()     → دقیقاً همان moves تأییدشده → تراکنش‌های transfer معمولی
                      + یک batch رکورد برای Restore
restore_previous()  → آخرین batch را با transferهای معکوس برمی‌گرداند —
                      خودش هم یک batch جدید (audit trail کامل) است
forecast_pond_view() → فرافکنی به‌جلو (بدون فروش)، فقط برای نمایش
```

الگوریتم: حریصانهٔ **دومرحله‌ای** (نه MILP) —
۱) نیاز *امروز* هر cohort (هرگز فدای رزرو آینده نمی‌شود)،
۲) با ظرفیت آزاد باقی‌مانده، آماده‌سازی برای نیاز *آیندهٔ* (peak تا
۱۲۰ روز) — زودترین نیاز اول.

## دو باگ واقعی که حین توسعه پیدا و رفع شد

1. **برون‌یابی نمایی منحنی رشد** — همان مشکلی که در کار Saleability هم
   دیده شد؛ برای cohortهای از قبل بالای ۱۵ گرم (مثل C01 واقعی، ۴۶.۹ گرم)،
   فرافکنی ۱۲۰ روزهٔ بدون‌حد باعث می‌شد پیش‌بینی ۱۷۱۶ گرم و نیاز به ۳۷ استخر
   نشان دهد. رفع شد: برای cohortهای فراتر از بالاترین وزن calibrate‌شده
   (`planning.harvest_weights`)، وزن برای برنامه‌ریزی استخر ثابت فرض
   می‌شود.
2. **نقص ترتیب در الگوریتم حریصانه** — مرتب‌سازی اولیه بر اساس نیاز
   *آینده* باعث می‌شد یک cohort با رشد آیندهٔ زیاد، استخرهایی را بگیرد که
   یک cohort دیگر همان *امروز* لازم داشت. با تفکیک قطعی به دو پاس رفع شد.

## نتیجه تست‌ها

**۲۹۴ تست · ۲۹۴ Pass · ۳ Fail** (اجرای کامل `tests/`).

هر ۱۱ تست جدید `test_pond_optimize.py` پاس شدند، شامل هر ۸ سناریوی
الزامی specification.

### سه شکست — تأیید شد کاملاً نامرتبط به این قابلیت

`test_egg_offer_reports_capacity_and_cash_impact`,
`test_stage1_and_2_still_pass`, `test_regression_plan_still_solves` —
هر سه به یک چک واحد برمی‌گردند: `plan_pond_feasible` («اوج نیاز ۲۰ استخر
از ۱۹ عملیاتی بیشتر است»). این همان مسئلهٔ حساسیت به گذر زمان واقعی
(date drift) است که در نشست قبلی (Demand Capacity) یک‌بار رفع شده بود؛ چون
`state.as_of` روزانه با تاریخ واقعی جلو می‌رود، cohortهای واقعی کمی رشد
کرده‌اند و دوباره از مرز عبور کرده‌اند.

**اثبات نامرتبط‌بودن:** با حذف کامل موقت `pond_optimize` از `app.py`
(import و هر چهار endpoint) و اجرای دوباره، همان سه شکست با همان مقادیر
دقیق تکرار شد. طبق دستور «فقط همین قابلیت را اضافه کن»، به این مسئلهٔ
نامرتبط دست زده نشد.

## ۸ سناریوی الزامی — همه پاس

1. `test_large_cohort_split_without_exceeding_capacity`
2. `test_large_weight_gap_never_mixed`
3. `test_small_weight_gap_can_be_mixed_when_needed`
4. `test_optimizer_prefers_current_placement_over_naive_reassignment`
5. `test_sub_1g_cohort_shows_in_future_forecast`
6. `test_date_selector_shows_different_future_states`
7. `test_restore_reverts_exactly_to_pre_apply_snapshot` +
   `test_restore_itself_has_audit_trail`
8. `test_apply_never_runs_without_explicit_confirmed_moves`

## اعتبارسنجی UI (سرور واقعی + Playwright)

جریان کامل با کروم واقعی تست شد: Optimize → Preview (۱۹ جابه‌جایی، آمار
کامل) → Apply (پند‌گرید واقعاً به‌روزرسانی شد) → Restore (دقیقاً به حالت
قبل برگشت) → Date Selector +۹۰ روز (banner Forecast + پیش‌بینی صحیح
grow-out برای C04/C05).
