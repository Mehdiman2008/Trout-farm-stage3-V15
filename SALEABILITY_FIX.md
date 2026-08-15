# اصلاح Market Saleability / Demand Liquidity

اصلاح محدود روی موتور بهینه‌سازی موجود؛ هیچ Stage یا معماری جدیدی اضافه نشد.

## خلاصه مشکل

Saleability از قبل در معماری اصلی وجود داشت (`sale_waves()` در
`plan_model.py`، مصرف‌شده در `existing_profile`/`new_lot_profile` که Plan،
Targets و rolling re-optimization از آن‌ها می‌سازند) — اما به‌صورت «فقط
تأخیر، بدون هیچ هزینهٔ واقعی» مدل شده بود. چون تابع هدف MILP اسمی است
(بدون تنزیل زمانی) و تأخیر در نهایت با قیمت کامل فروش تضمین می‌شد، هزینهٔ
واقعیِ انتخاب وزن بالا با سهولت فروش پایین تقریباً صفر بود؛ در نتیجه قیمت
اسمی بالاتر در ۱۰–۱۵ گرم می‌توانست به‌تنهایی مدل را به نگه‌داشتن بیش‌ازحد
سوق دهد.

علاوه بر این، دو کپی واگرا از جدول احتمال فروش وجود داشت: یک بلوک کاملاً
مرده در `config.yaml` (هیچ‌جا مصرف نمی‌شد) و یک فرمول جدا در Offer
Evaluator که `saleability` را فقط نمایش می‌داد، نه محاسبه می‌کرد.

## فایل‌های تغییرکرده

| فایل | تغییر |
|---|---|
| `config.yaml` | حذف بلوک مردهٔ `saleability.*`؛ `planning.saleability` از ۲ بازه به ۵ بازه گسترش یافت + فیلد `retry_discount`؛ آستانهٔ جدید `planning.saleability_warn_threshold` |
| `core/plan_model.py` | تابع مشترک `saleability_band(A, w)` (تنها منبع حقیقت)؛ `_simulate()` اصلاح شد تا تلاش اول قیمت کامل و تلاش‌های تأخیری تخفیف بازه بگیرند |
| `core/planner.py` | هشدار `saleability_warnings` در `summary()`، از همان `sales_by_weight` موجود |
| `core/validate.py` | چک `plan_saleability` به `warn` تغییر می‌کند وقتی bottleneck شناسایی شود |
| `core/offers.py` | `_best_alternative` بازنویسی شد تا از `saleability_band` مشترک بخواند (نه فرمول جدا)؛ تابع تکراری `_saleability` حذف شد |
| `tests/test_stage2_final.py`, `tests/test_stage3_fixes.py` | تست‌های قدیمی که فرض ۲-بازه‌ای/بدون‌تخفیف را hard-code داشتند به‌روزرسانی شدند؛ ۴ تست رگرسیون جدید اضافه شد |

## نتیجه تست‌ها

**۲۸۲ تست · ۲۸۲ Pass · ۰ Fail** (اجرای کامل و یکپارچهٔ `tests/`).

## تست‌های کلیدی جدید

- `test_15g_higher_price_no_longer_wins_purely_on_nominal_price` — با قیمت
  اسمی واقعاً بالاتر در ۱۵ گرم، این وزن دیگر برندهٔ per-fish contribution
  نیست.
- `test_saleability_shared_across_plan_and_offer_evaluator` — تأیید می‌کند
  `PlanModel.saleability` و `saleability_band` (مصرف‌شده در Offer
  Evaluator) دقیقاً همان یک تابع‌اند.
- `test_plan_summary_reports_sale_bottleneck_warning` — هشدار
  Unsold Inventory وقتی فروش در بازهٔ کم‌سهولت متمرکز شود.
- `test_saleability_low_band_gets_material_price_haircut` — اثر واقعی و
  اندازه‌گیری‌پذیر `retry_discount` روی درآمد واقعی‌شدهٔ ۱۵ گرم.

## اثر مشاهده‌شده روی داده واقعی

روی وضعیت فعلی مزرعه، حاصل‌ضرب کل برنامهٔ ۱۲‌ماهه (balanced) از حدود
۱۰٫۲ میلیارد به حدود ۷٫۳ میلیارد تومان تغییر کرد — نشانهٔ درست کار کردن
اصلاح (مدل قبلی سود کاذب از نگه‌داشتن بیش‌ازحد در وزن‌های سنگین با بازار
محدود نشان می‌داد)، نه یک رگرسیون. جزئیات کامل در گزارش قبلی گفت‌وگو آمده
است.

## Assumptionهای باقی‌مانده (صریح)

- ۵ مقدار `prob` و ۵ مقدار `retry_discount` — برآورد کیفی مهندسی مزرعه،
  نه Observed Market Fact.
- `expected_days_to_sale` — عمداً *محاسبه‌شده* از `prob` + آهنگ تلاش مجدد
  سراسری، نه یک عدد جدای per-band (تا با شبیه‌سازی واقعی واگرا نشود).
- `expected_sellable_quantity/demand_capacity` مطلق — **عمداً پیاده
  نشد**؛ داده واقعی کافی نداریم و ساختن عدد تخیلی خواسته نشده بود.

داده لازم برای یادگیری واقعی این پارامترها (وزن، تعداد، قیمت درخواستی/
پیشنهادی، تعداد فروخته‌شده، تاریخ فروش، روز تا فروش، تخفیف، خریدار، شرایط
پرداخت) از قبل در تراکنش‌ها/Offerها ثبت می‌شود؛ فقط الگوریتم تخمین ساخته
نشده — طبق دستور، ML اضافه نشد.
