# داشبورد زنده مزرعه قزل‌آلا — مرحله ۱

سیستم تصمیم‌یار عملیاتی برای مزرعه پرورش بچه‌ماهی قزل‌آلا.
**مرحله ۱** — Live Farm State، persistence، transaction history، شماتیک
۲۱ استخر، تخمین خودکار cohortها، تفکیک Actual/Estimated/Forecast، تب
فرضیات و بنچمارک سه‌ماهه ارزی.

**مرحله ۲** — Rolling Plan دوازده‌ماهه با MILP، اهداف ماهانه و سه‌ماهه،
برنامه اقدام ۹۰ روزه، Target vs Actual، سه حالت مدیریتی و سناریوهای حجم تخم.

**مرحله ۳** — موتور تصمیم آفر: ارزیابی آفر خرید تخم با حداکثر قیمت
توجیه‌پذیر، ارزیابی آفر فروش با سه قیمت مرجع، و سناریوهای What-If.

مرحله ۴ (یادگیری تطبیقی و لایه AI) هنوز شروع نشده است.

---

## اجرا

```bash
pip install -r requirements.txt      # فقط PyYAML و openpyxl
python3 app.py                        # http://127.0.0.1:8000
```

گزینه‌ها:

```bash
python3 app.py --port 8080            # تغییر پورت
python3 app.py --seed-demo            # افزودن داده نمونه [DEMO]
python3 app.py --reset                # پاک کردن پایگاه داده و شروع از نو
python3 -m pytest tests/ -q           # اجرای تست‌ها
```

بار اول، شش خرید واقعی تخم (مجموع ۸۵۰٬۰۰۰ عدد) به‌عنوان `Observed Data`
بارگذاری می‌شوند و ۲۱ استخر ساخته می‌شود.

### فایل نرخ دلار

فایل واقعی `TGJU_USD_3Y_Daily_Weekly_Close(1).xlsx` در پوشه `data/` قرار
دارد و بارگذاری می‌شود: شیت `روزانه`، ستون `قیمت پایانی (تومان)` —
۸۵۹ رکورد از ۲۰۲۳-۰۸-۱۰ تا ۲۰۲۶-۰۸-۰۹.

**هیچ داده جایگزین یا ساختگی ساخته نمی‌شود.** اگر فایل نباشد یا یک دوره
داده نداشته باشد، همان دوره «داده موجود نیست» نمایش داده می‌شود.

### پشتیبان‌گیری

از دکمه «پشتیبان‌گیری» در نوار بالا یا تب «کیفیت داده»:

| مسیر | خروجی |
|---|---|
| `/api/export/sqlite` | پشتیبان کامل SQLite (backup API، سازگار با WAL) |
| `/api/export/json/download` | JSON کامل همه جداول |
| `/api/export/csv?table=transactions` | CSV تراکنش‌ها (با BOM برای اکسل فارسی) |

پشتیبان شامل cohortها، استخرها، تراکنش‌ها، خوراک، مشاهدات، فرضیات،
تاریخچه قیمت و کل audit trail است.

---

## معماری

```
config.yaml            همه پارامترها با برچسب OBS / MA / DER  (Fix 5)
core/assumptions.py    رجیستری فرضیات: config.yaml ⊕ override های کاربر
core/biology.py        رشد، تلفات، ناهمگنی وزن، ظرفیت، خوراک، قیمت
core/db.py             SQLite؛ transaction history فقط append
core/state.py          بازپخش تاریخچه → Live Farm State
core/ledger.py         دفتر نقدی و سرمایه در گردش  (Fix 4)
core/forecast.py       پیش‌بینی ظرفیت، milestone، timeline، خوراک  (Fix 2)
core/fx.py             بنچمارک سه‌ماهه ارزی
core/seed.py           بارگذاری داده واقعی + داده نمونه اختیاری
core/plan_model.py     پروفایل‌های هفتگی و موج‌های برداشت (مرحله ۲)
core/optimizer.py      MILP روی PuLP/CBC + سه حالت مدیریتی
core/planner.py        تجمیع ماهانه/سه‌ماهه، برنامه ۹۰ روزه، پرچم ریسک
core/variance.py       Target vs Actual
core/plan_cash.py      دفتر نقدی برنامه‌ریزی‌شده و سرمایه در گردش
core/pond_alloc.py     تخصیص خودکار استخر (Suggested → Actual)
core/attribution.py    تشخیص cohort فروش و تقسیم چند-cohort
core/offers.py         موتور تصمیم خرید و فروش (مرحله ۳)
core/attribution.py    تشخیص cohort یک فروش از روی وزن فروخته‌شده
core/validate.py       ۱۴ بررسی کیفیت داده + ۱۰ بررسی کیفیت برنامه
app.py                 سرور HTTP + API (فقط کتابخانه استاندارد)
static/                داشبورد فارسی RTL؛ بدون build step
data/farm.db           پایگاه داده (خودکار ساخته می‌شود)
```

**اصل مرکزی:** وضعیت جاری مزرعه هیچ‌وقت ذخیره نمی‌شود؛ همیشه از بازپخش
تراکنش‌ها ساخته می‌شود. بنابراین تغییر فرضیات فقط تخمین‌ها را عوض می‌کند
و هیچ داده تاریخی را دست نمی‌زند، و هر عدد قابل ردیابی و reproducible است.

---

## تفکیک منابع داده

| برچسب | معنی | نمونه |
|---|---|---|
| **Actual / Observed** | ثبت واقعی توسط کاربر یا داده تثبیت‌شده | خرید تخم، وزن‌کشی، فروش |
| **Estimated** | محاسبه‌شده از داده واقعی + منحنی‌های مدل، برای «الان» | تعداد زنده امروز، وزن متوسط امروز |
| **Forecast** | فرافکنی به آینده | نیاز ظرفیت ۹۰ روز آینده |
| **Modelling Assumption (MA)** | فرض قابل تغییر | CV پراکندگی وزن، front-load تلفات |

قواعد:

* داده Actual هرگز با Estimated بازنویسی نمی‌شود.
* اصلاح تراکنش = درج نسخه جدید + علامت‌گذاری نسخه قبلی به‌عنوان
  `corrected`؛ هیچ رکوردی حذف نمی‌شود.
* آخرین `weight_sample` مبنای رشد را جابه‌جا می‌کند؛ forecast از همان
  نقطه ادامه می‌یابد (`growth_offset_days`).
* آخرین `count_observation` تعداد را به‌صورت مطلق تثبیت می‌کند.
* با کلیک روی هر استخر در شماتیک، پنل «ثبت داده واقعی» باز می‌شود:
  تعداد، وزن نمونه و انحراف معیار، تلفات، زیست‌توده، دما، DO ورودی/خروجی،
  دبی، خوراک مصرفی و یادداشت. هر فیلد پرشده به یک transaction با برچسب
  Actual تبدیل می‌شود و تفاوت Estimated ← Actual بلافاصله نمایش داده می‌شود.
* ویرایش یک رویداد قبلی نسخه جدید می‌سازد؛ نسخه قبلی با مقدار اولیه،
  تاریخ اصلاح و دلیل در تاریخچه می‌ماند و از دکمه «تاریخچه» قابل دیدن است.
* رکوردهای `mortality` فقط در بازه‌ای که واقعاً گزارش شده‌اند جایگزین
  منحنی مدل می‌شوند، نه به‌صورت بازگشتی برای کل گذشته.

---

## مدل ناهمگنی رشد

هر cohort یک `mean_weight` دارد و وزن تک‌ماهی‌ها حول آن با توزیع
**Lognormal** مدل می‌شود:

```
σ = √(ln(1 + CV²))          μ = ln(mean) − σ²/2
```

CV به‌صورت خطی (روی محور لگاریتم وزن) از `cv_at_1g = 12٪` تا
`cv_at_15g = 18٪` زیاد می‌شود — هر دو `Modelling Assumption` و قابل
تغییر از تب فرضیات. سه گروه قابل فهم نمایش داده می‌شود:

* **کندرشد** = P10 · **معمول** = P50 · **تندرشد** = P90

با ثبت یک `weight_sample` که انحراف معیار دارد، CV همان cohort با داده
واقعی به‌روزرسانی می‌شود (`cv_shift`).

تابع `fraction_above(w)` سهمی از cohort را که از یک وزن عبور کرده
برمی‌گرداند — پایه‌ای برای grading و partial harvest در مراحل بعد.

---

## سرمایه در گردش — سه عدد مجزا

| عدد | معنی |
|---|---|
| **موجود / فرض پایه** | `finance.working_capital_available` = ۸ میلیارد تومان. یک **ورودی مرجع** قابل تغییر در تب فرضیات، نه خروجی ثابت optimizer. |
| **قفل‌شده هم‌اکنون** | بهای تمام‌شده موجودی زنده + ارزش انبار خوراک، از تراکنش‌های واقعی. |
| **اوج پیش‌بینی‌شده موردنیاز** | projection رو به جلو (پیش‌فرض ۱۸۰ روز): خوراک رشد، هزینه ثابت، تعهدات سررسیدنشده و فروش مورد انتظار در وزن برداشت پایه. |

هر تراکنش واقعی جدید — تخم، خوراک، فروش، هزینه، تغییر payment terms یا
قیمت — هر سه عدد را بازمحاسبه می‌کند.

## هزینه‌های جانبی و double counting

هزینه‌های واقعی با دسته‌بندی (`نگهداری و تعمیرات`، `برق و سوخت`، `دستمزد`،
`حمل و نقل`، `دارو و بهداشت`، `اداری`، `سایر`) ثبت می‌شوند.
پارامتر `cost.fixed_cost_mode` تعیین می‌کند چگونه با هزینه ثابت پایه
(۱۲۰ م/ماه) ترکیب شوند:

* **`top_up`** (پیش‌فرض) — در هر ماه فقط مابه‌التفاوت تا هزینه پایه اضافه می‌شود
* **`baseline_only`** — فقط پایه؛ هزینه‌های واقعی جداگانه شمرده نمی‌شوند
* **`actual_only`** — فقط هزینه‌های واقعی ثبت‌شده

## تاریخ اعتبار قیمت‌ها

پارامترهای `egg.base_price`، `feed.price_table`، `price.base_1g`،
`price.slope_per_gram` و `cost.fixed_monthly` **effective-dated** هستند.
تغییر قیمت هرگز retroactive نیست:

```
قیمت خوراک ۳۰۰٬۰۰۰ تا ۳۰ سپتامبر · ۳۲۰٬۰۰۰ از ۱ اکتبر
→ تراکنش‌های قبل از ۱ اکتبر با قیمت تاریخی خودشان می‌مانند
```

اولویت قیمت: **قیمت واقعی ثبت‌شده در تراکنش > مقدار effective در آن تاریخ >
مقدار جاری assumption**. تاریخچه هر پارامتر در تب فرضیات دیده و ویرایش می‌شود.

## پنج اصلاح specification در این مرحله

| اصلاح | وضعیت در مرحله ۱ |
|---|---|
| **Fix 1** Offer-based procurement | جدول `egg_offers` + جریان پذیرش/رد/پذیرش جزئی؛ آفر رد شده حفظ می‌شود |
| **Fix 2** Non-cyclic timeline | تمام محاسبات روی تقویم واقعی؛ هیچ modulo-52 در کد نیست |
| **Fix 3** Optimizer-based forced sale | مربوط به مرحله ۲/۳؛ اینجا فقط فرض پایه برداشت شفاف و قابل تغییر است |
| **راهنمای ماهانه تخم** | ۳۰۰٬۰۰۰ یک Planning Guideline است، نه محدودیت فیزیکی؛ سقف سخت اختیاری و خاموش |
| **Fix 4** Post-solve WC ledger | `core/ledger.py` دفتر نقدی واقعی از تراکنش‌ها می‌سازد |
| **Fix 5** Stochastic assumptions in config | همه در گروه `stochastic` در `config.yaml` |

---

## API

| مسیر | توضیح |
|---|---|
| `GET /api/bootstrap` | کل داده داشبورد در یک درخواست |
| `GET /api/ponds` · `/api/cohorts` · `/api/feed` | نماهای جزئی |
| `GET /api/forecast` · `/api/ledger` · `/api/fx` | پیش‌بینی، نقدینگی، ارز |
| `GET/POST /api/transactions` | تاریخچه و ثبت رویداد |
| `POST /api/transactions/{id}/correct` · `/void` | اصلاح با audit trail |
| `GET/POST /api/assumptions` · `POST /api/assumptions/reset` | تب فرضیات |
| `GET /api/offers` · `POST /api/offers/egg` · `/api/offers/egg/decide` | Fix 1 |
| `GET /api/pond/{id}` · `POST /api/pond/observe` | پنل استخر و ثبت داده واقعی |
| `GET /api/transactions/{id}/history` | زنجیره کامل audit trail |
| `POST /api/assumptions/effective` · `GET /api/assumptions/history` | تاریخ اعتبار قیمت |
| `GET /api/export/sqlite` · `/json/download` · `/csv?table=` | پشتیبان‌گیری |
| `GET /api/sales/unassigned` · `/api/sales/{id}/suggest` | تشخیص cohort از روی وزن فروش |
| `POST /api/sales/{id}/assign` · `/implied-cohort` | تخصیص کاربر و ثبت cohort استنتاجی |
| `GET /api/plan?variant=` | برنامه کامل: ماهانه، سه‌ماهه، ۹۰ روزه، انحراف |
| `GET /api/plan/variants` · `/scenarios` | مقایسه سه حالت و سناریوهای حجم تخم |
| `POST /api/plan/save` · `GET /api/plan/variance` | ثبت برنامه پایه و Target vs Actual |
| `POST /api/decide/egg` · `/sale` · `/what-if` | موتور تصمیم (مرحله ۳) |
| `GET /api/decide/context` · `/api/solver` | وضعیت لحظه‌ای و سلامت حل‌کننده |
| `GET /api/validate` | ۱۴ بررسی کیفیت |

همه endpointها `?as_of=YYYY-MM-DD` را می‌پذیرند تا وضعیت مزرعه در یک
تاریخ گذشته بازسازی شود.


---

## مرحله ۲ — برنامه‌ریزی

برنامه با MILP (PuLP/CBC) ساخته می‌شود. متغیرها: خرید lot در هر ماه از
فهرست گسسته اندازه‌های واقعی، و سهم هر cohort موجود در هر وزن برداشت.
چون سهم پیوسته است، **برداشت جزئی و grading** قابل انتخاب است و همان
Optimizer-Based Forced Sale مورد نظر Fix 3 را می‌دهد.

محدودیت ظرفیت استخر و سرمایه در گردش **کشسان** هستند: اگر وضعیت امروز
خودش از ظرفیت عبور کرده باشد، مدل به‌جای Infeasible شدن کمبود را کمینه و
صریح گزارش می‌کند.

سه حالت مدیریتی: بیشینه سود (λ=۰) · متعادل (λ=۰٫۳) · محافظه‌کارانه (λ=۰٫۶)،
که در آن λ وزن سناریوی نامساعد در تابع هدف است. سناریوی نامساعد از
پارامترهای گروه `stochastic` در config ساخته می‌شود (Fix 5).

اگر PuLP نصب نباشد، برنامه‌ریز به‌صورت خودکار به یک الگوریتم حریصانه امن
برمی‌گردد و خروجی همان ساختار را دارد.


---

## اصلاحات نهایی مرحله ۲

* **تخصیص خودکار استخر** — cohortهای بالای ۱ گرم پیشنهاد استخر می‌گیرند؛
  تا تأیید مدیر `Suggested` می‌مانند و با تأیید به تراکنش `transfer` و
  وضعیت `Actual` تبدیل می‌شوند.
* **تشخیص cohort فروش** — از وزن، تاریخ، سن و موجودی. اگر هیچ cohortی
  نخواند، تاریخ خرید ضمنی محاسبه می‌شود؛ حدس زده نمی‌شود.
* **فروش چند-cohort** — جدول `sale_allocations` با قید
  `Σ تخصیص = تعداد کل فروش` و سقف موجودی هر cohort.
* **شرایط پرداخت مشتری** — ۵۰٪ نقد، ۵۰٪ پس از ۴۵ روز. تاریخ شناسایی درآمد
  از تاریخ دریافت وجه جداست و همین تفاوت اوج نیاز نقدی را می‌سازد.
* **احتمال یافتن مشتری** — ۷۰٪ زیر ۵ گرم، ۳۰٪ بالای آن. هرگز در قیمت ضرب
  نمی‌شود؛ به‌صورت تأخیر فروش مدل می‌شود و روی اشغال استخر، خوراک، تلفات و
  مدت درگیری سرمایه اثر می‌گذارد.
* **وزن فروش ۱ گرم** به گزینه‌های optimizer اضافه شد.
* **دو KPI جدا** — نتیجه غلتان ۱۲ ماهه در برابر ارزش کل چرخه عمر. عدد
  چرخه عمر «سود سالانه» نامیده نمی‌شود.
* **ظرفیت صحیح استخر** — حلقه `Solve → validate → برش ظرفیت → re-solve`.
  برنامه‌ای که پس از گرد کردن از ۱۹ استخر عبور کند feasible اعلام نمی‌شود.
* **PuLP الزامی است** — اگر نصب نباشد داشبورد و ثبت داده کار می‌کنند ولی
  optimizer اجرا نمی‌شود و نتیجه heuristic به‌جای آن نمایش داده نمی‌شود.


---

## مرحله ۳ — موتور تصمیم

**آفر تخم:** ارزش هر lot حاشیه‌ای سنجیده می‌شود — بهترین برنامه *با* آن در
برابر بهترین برنامه *بدون* آن. اگر lot را با قیمت صفر وارد کنیم، اختلاف
دو برنامه تقسیم بر تعداد تخم، همان **حداکثر قیمت توجیه‌پذیر** است. بالاتر
از آن، خرید سود کل مزرعه را کم می‌کند حتی اگر خودِ lot سودده به نظر برسد.

**آفر فروش:** سه قیمت مرجع — کف حسابداری (بهای تمام‌شده تاریخی)، کف تصمیم
اقتصادی (قیمتی که فروش امروز با نگه‌داشتن برابر می‌شود) و قیمت پیشنهادی
متقابل. کف اقتصادی عمداً هزینه گذشته را نادیده می‌گیرد چون sunk است.

**What-If:** سناریو بدون تغییر داده واقعی؛ فرضیات موقتاً عوض و حتماً
برگردانده می‌شوند.

## Live USD/Toman FX service (Stage 3.2 / Navasan)

Live verified closing prices use the Navasan OHLC API. Historical TGJU Excel remains the historical backfill, validation and quarterly benchmark source.

Configure deployment environment variables rather than editing source code:

- `NAVASAN_API_KEY` — required API key; never commit it to source control
- `NAVASAN_API_BASE_URL` — optional, defaults to `https://api.navasan.tech`
- `NAVASAN_ITEM` — optional, defaults to `usd_sell` (Tehran USD sell)
- `NAVASAN_LOOKBACK_DAYS` — optional, defaults to `10`
- `NAVASAN_VALUE_DIVISOR` — optional, defaults to `1` because Navasan market values are treated as Toman

The service calls Navasan `ohlcSearch` and deliberately requests only completed calendar days (up to yesterday in Iran time). This prevents an intraday quote from being labelled as a verified closing price. If no newer completed close exists, the dashboard keeps the latest verified previous close and displays its age/stale status.

At application startup the service attempts at most one live refresh per calendar day. Manual observations remain the lowest-priority fallback. Data priority for the same date is Navasan API > historical TGJU Excel > manual. The USD quarterly capital benchmark remains an opportunity-cost/capital-value benchmark only and does not replace operating profit, NPV or the optimiser objective.
