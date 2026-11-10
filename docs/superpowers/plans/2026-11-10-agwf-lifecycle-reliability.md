# AGWF Başlangıç ve Toparlanma Güvenilirliği Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** İlk başlatma hatasından güvenli yeniden denemeyi tamamlamak, ölçüm yenilemeyi zamanında işletmek ve gerçek devam/kapanış davranışlarını doğru testlerle kanıtlamak.

**Architecture:** Mevcut pinned candidate, workflow ve model kullanmayan watcher korunacak. İlk etkinleştirme için candidate'a küçük bir deneme günlüğü ve güvenli tekrar sözleşmesi eklenecek; yeni bir orchestrator veya ikinci Codex bağlantısı açılmayacak. Tekrar kullanılabilir kabul başlatıcısı, hazırlık/gerçek ölçüm/çalışma/kapanış aşamalarını ayıracak; süreli test modu normal akıştan açıkça ayrı kalacak.

**Tech Stack:** Python 3.11+, standart kütüphane, unittest, mevcut Codex goal/usage araçları, Windows süreç ve dosya paylaşımı davranışı.

**Spec:** `docs/superpowers/specs/2026-11-10-agwf-lifecycle-reliability.md`

## Global Constraints

- Kullanıcı bu planın uygulanmasını ve v1.1 değişikliklerinin GitHub'a commit/push edilmesini yetkilendirdi. GitHub Release veya release tag oluşturulmayacak.
- Python 3.11+ ve standart kütüphane; yeni servis, veritabanı, npm paketi veya ikinci Codex bağlantısı yok.
- Ölçüm zamanını/limitlerini, kullanıcı alıntısını, native durum geçişini veya süreç kimliğini üretme; sentetik verileri yalnız açıkça izole testlerde kullan.
- `usage_receipt_expired` ile gerçek kota/erken rezerv/kullanıcı duruşu ayrı kalacak. Eski SAVE kanıtı korunacak; yeni run ile duruş atlanmayacak.
- Başka sohbetlerin hedeflerini, model/effort ayarlarını ve yetkilerini değiştirme. Normal akışta süreli veya yapay SAVE üretme.
- Şimdiki staged/unstaged değişiklikleri ve kişisel raporları koru. Uygulama başlangıcında güncel onaylı kaynak durumunu yalıt; HEAD'in mevcut düzeltmeleri içerdiğini varsayma. Yalnız ilgili değişiklik parçaları commit kapsamına alınabilir.
- Paket pin'i ve çalışma bağı korunacak; canlı worker kullanılan kurulum yerinde değiştirilmeyecek. Eski koşumun otomatik taşınmış sayılması bu planın sonucu değildir.

## Review Focus

1. İlk başlatma hatasından sonra workflow henüz yokken watcher SAVE kaydı bulunması: taze veriyle aynı kapsamda tekrar denenebilir olmalı. Görev 1.
2. Başlatma ile tekrar deneme arasına gerçek kota duruşu, kullanıcı duruşu, hedef değişimi veya belirsiz worker girmesi: teknik tekrar bunları aşmamalı. Görev 1 ve 4.
3. Bir denemenin yarıda kalması veya ikinci çağrının aynı anda gelmesi: bilinmeyen sonuç kendiliğinden başarıya dönüştürülmemeli. Görev 1.
4. Dosya yayını yeni olduğu hâlde ölçümün eski olması; taze sample varken lease'in bitmiş olması: bağımsız sağlık koşulları korunmalı. Görev 2 ve 4.
5. Native olayın geç gözlenmesi ve Windows geçici dosya kilidi: geç başlatılan test zamanında çalışmış sayılmamalı, okunamayan kayıt süreç çıkışı sayılmamalı. Görev 3 ve 4.

## Dosya ve sorumluluk haritası

Aşağıdaki yollar depo köküne göredir.

| Dosya | Sorumluluk |
|---|---|
| `skills/agent-workflow-v1-1/scripts/candidate.py` | Başlatma denemesi, mevcut yetkiyle tekrar kararı, worker işlemi ve sonuç kaydı |
| `skills/agent-workflow-v1-1/private/limit-watcher/scripts/monitor.py` | Mevcut SAVE/epoch/receipt kuralları; yalnız sözleşmenin gerektirdiği dar değişiklikler |
| `skills/agent-workflow-v1-1/scripts/workflow.py` | Mevcut aktif/halted/complete ve kullanıcı duruşu invariants; yeniden tasarım yok |
| `tooling/run-lifecycle-check.py` — yeni | Önceden hazırlanmış, olay/receipt girdili kabul testi başlatıcısı; native araçları kendisi çağırmaz |
| `tooling/tests/test_lifecycle_check.py` — yeni | Hazırlık, geç olay, zamanlama ve sonuç sınıflandırması |
| `skills/agent-workflow-v1-1/tests/test_candidate_coexistence.py` | Gerçek kaynak/paket girişleri ve gerçek sahipli Python worker ile entegrasyon |
| `tests/test_native_usage_source.py`, `test_goal_binding.py`, `test_watcher_engine.py`, `test_monitor.py`, `test_watcher_atomic_write.py` — skill test dizininde | Gerçek ölçüm sınırı, yetki/goal, SAVE, timer ve Windows sözleşmelerinin regresyonları |
| `SKILL.md`, `references/operations.md`, `references/native-usage.md`, `references/coexistence.md` — skill dizininde | Ana ajanın yenileme/tekrar/dağıtım adımları ve doğrulama sınırları |

## Görev 1 — İlk etkinleştirme başarısızlığından güvenli yeniden deneme

**Files:** Modify `skills/agent-workflow-v1-1/scripts/candidate.py`; gerektiğinde dar kapsamla private `monitor.py`. Test `test_candidate_coexistence.py` ve `test_native_usage_source.py`.

**Interfaces:** `activation_identity(command: str, payload: dict) -> dict`; `record_activation_attempt(runtime, record: dict) -> None`; `startup_retry_options(runtime, engine, command: str, payload: dict) -> tuple[str, ...]`. Günlük `<run>/activation-attempt.json`; mevcut çalışma bağına ait `schema_version`, `command`, kapsam kimliği, `pre_state_sha256`, `phase`, gerçek watcher epoch/diagnostic/receipt referansı ve sonuç zamanı taşır. `phase`: `prepared`, `failed`, `committed`; belirsiz `prepared` için otomatik başarı/tekrar yok.

- [ ] Eski ölçümle ilk `bind-active-goal` başarısız olsun; workflow etkinleşmemiş ve worker yokken aynı run/goal/scope için taze gerçek-biçimli test receipt'iyle ikinci çağrı sağlıklı `active` üretsin: `test_initial_expiry_then_fresh_same_goal_retries_without_new_quote`. İlk SAVE dosyasının hash'i korunsun; yeni yetki olayı üretilmesin. Önce mevcut kodda başarısızlığı gör.
- [ ] Aynı testi `activate` yoluna da ekle; geçerli güncel yetki sözleşmesini ve orijinal kaynak zamanını koru. Süresi dolmuş kullanıcı yetkisini bu özellik yenilemesin.
- [ ] `preflight_activation` geçtikten sonra deneme niyetini atomik kaydet. Kapsam kimliğine command, run/goal/thread/scope, objective ve yetki/binding kimliği girsin; değişen gözlem zamanı kapsam değişimi sayılmasın. Beklenmedik eski kimlik, duruş geçmişi veya paket uyuşmazlığı tekrar yetkisi sayılmasın.
- [ ] Mevcut launcher/core kilitleri arasındaki boşluk için candidate lifecycle işlemlerini kapsayan sahipli tek-yazıcı kilidi kullan. Kilit sahibi belirsizken işlemi reddet; PID kill veya kör kilit silme ekleme. Native UI ile atomik transaction varmış gibi davranma.
- [ ] Yalnız bu günlüğün doğruladığı ilk etkinleştirme hatasında, gerçekten `usage_receipt_expired` olmuş aynı watcher/run ve yeni aktif goal/receipt ile mevcut teknik epoch mekanizmasını kullan. Caller'ın `retry=true` demesi kanıt olmasın. Her tekrar fresh preflight'tan geçsin; başarı yalnız sağlıklı worker ve core commit sonrası yazılsın.
- [ ] Negatifleri ekle: kota/erken rezerv/başka diagnostic, yeni SAVE, pasif/değişmiş goal, açık kullanıcı duruşu, canlı/bilinmeyen worker, değiştirilmiş günlük, paket uyuşmazlığı, yarım `prepared`, eşzamanlı iki başlatma. Hepsi yeni iş başlatmadan reddetsin; yeni SAVE ve yetki geçmişi korunsun.
- [ ] Bozuk ilk retry receipt'i sonrası geçerli retry geçsin; rollback yalnız doğrulanmış eski kontrolü geri koysun, yeni gerçek SAVE'i geri almasın. Kanıtsız eski başlangıç kaydı otomatik etiketlenmesin.
- [ ] Çalıştır: `python -X utf8 -B -m unittest discover -s skills/agent-workflow-v1-1/tests -p test_candidate_coexistence.py -v` ve ilgili native usage testleri. Gerçek worker testinde aktif state, canlı/fresh kimlik, eski SAVE hash'i ve son cleanup doğrulansın.

## Görev 2 — Hazırlığı ölçümden ayır; yenilemeyi son tarihten önce yap

**Files:** Create `tooling/run-lifecycle-check.py`, `tooling/tests/test_lifecycle_check.py`; modify candidate `watcher_summary` ve skill `references/native-usage.md`/`operations.md`.

**Interfaces:** Başlatıcı `prepare`, `run --input PATH`, `observe --input PATH`, `close` modları sunar. `prepare` paket/pin/rol/komutları hazırlar ve worker başlatmaz. `run/observe` host'un az önce aldığı gerçek goal ve usage receipt'ini okur. `refresh_by(at: float, interval: float) -> float` yalnız çalışma sınırı önerisidir: `at + interval - min(15, interval / 4)`; gerçek TTL/limit kararı değişmez.

- [ ] Testler önce: `prepare` süreç başlatmasın; `--help` girdi dosyası aramasın; `run` eksik/eskimiş input ile etkin state üretmesin; aynı receipt'in tekrar yayını gözlem zamanını yenilemesin. Eski canlı başlatıcıdaki hazırlık sırasını örnek al fakat geçici rapor script'lerini ürün bağımlılığı yapma.
- [ ] Paket/başlatıcı hazırlığını tamamladıktan sonra ana ajan gerçek goal/usage araçlarını çağırıp girdiyi hemen çalıştırsın. Arka plan süreci host-tool çağırmasın. Receipt kaynağında hesap/kredi/kimlik bilgilerini saklama; gerekli iki pencere ve reset zamanları yeterli.
- [ ] Candidate status özetine gerçek `sample_at`, `next_check_at`, `check_interval_seconds` ve hesaplanan `refresh_by` ekle. Bunlar yeni ölçüm veya otomatik yenileme iddiası olmasın. 60 saniyelik örnekte önerilen yenileme noktası 45 saniye; drain interval'i 1 saniyeye düşerse güvenlik payı interval'in dörtte birini aşmasın.
- [ ] Yenileme kabulü, yalnızca dosya yazımı/heartbeat değil `status.at == yeni_receipt.observedAt`, sağlıklı süreç kimliği, epoch eşleşmesi, `sampleFresh` ve `leaseValid` gerektirsin. `workflow usage --usage-input` ardından sağlıklı durumla lease yenilendiği ayrıca doğrulansın.
- [ ] Testler: taze sample + bitmiş lease sağlıklı sayılmasın; lease yenilemek eski sample'ı tazelemesin; uzun hazırlık ilk gerçek okumanın öncesinde kalsın; kalan süre bir sonraki işlem için yetersizse ana ajan yeni ölçüm alsın. Uzun kesilemeyen çağrı için garanti verilmesin.
- [ ] Normal modda timer/fault injection varsayılan kapalı olsun. Gerçek karar checkpoint istiyorsa restore kontrolü yap; iş bitince `workflow complete` ve worker yokluğunu doğrula. Normal kabul testinde SAVE latch oluşmadığını denetle.
- [ ] Çalıştır: `python -X utf8 -B -m unittest discover -s tooling/tests -p test_lifecycle_check.py -v` ve `test_native_usage_source.py`. Gözlem zamanı ile dosya yayın zamanı farklı fixture'lar kullan.

## Görev 3 — Zamanlama testini önceden hazırla ve geç koşumu doğru sınıflandır

**Files:** Modify yeni `tooling/run-lifecycle-check.py` ve `tooling/tests/test_lifecycle_check.py`; test prosedürü `references/operations.md`.

**Interfaces:** Yalnız açık `timed-test` modu; `--event-input PATH --delay-seconds 10 --tolerance-seconds 1`. Event girdisi gerçek goal kimliği, olay zamanı, gözlem zamanı ve saklanan native receipt referansını taşır. Üretimdeki goal araçlarını yalnız ana ajan kullanır; worker yalnız kendi test koşumunun candidate işlemlerini yürütür.

- [ ] `test_late_native_event_is_not_timing_success`: olaydan 45 saniye sonra gelen girdi `INVALID_TIMING` üretsin; derhal stop edip bunu 10 saniyelik başarı diye yazmasın. 98,74 ve 45,05 saniyelik eski canlı sonuçlar başarısız kalmalı.
- [ ] İyi senaryoda başlatıcı olay gelmeden hazırlanmış olsun; gerçek olayı alıp workflow'u deadline'dan önce sağlıklı açabilsin. Deadline `event_at + 10` olsun; beklemede monotonic clock kullanılsın. Arayüzden olayın gelmesi garanti edilemiyorsa hazır/garantili zaman testi diye sunma.
- [ ] Olay kaynağı/latency sınırı: mevcut resmî goal aracı kullanılsın. Daha hızlı resmî event hook varsa önce belgelenmiş desteği yerelde doğrula; özel app-server, DB veya kimlik bilgisi okuyarak zamanlama altyapısı oluşturma. Böyle bir hook bulunmadığında geç gözlenen native olay testi geçersiz sayılır.
- [ ] `native_event_at`, `observed_at`, `worker_healthy_at`, `save_requested_at`, `save_returned_at`, `snapshot_verified_at`, `helper_halted_at` ve `native_paused_at` ayrı kaydedilsin. Native pause gecikmesi, Python SAVE zamanıyla gizlenmesin. Başarı için gerçek SAVE isteği olaydan 9–11 saniye aralığında ve worker öncesinde sağlıklı olmalı.
- [ ] İki tur aynı goal/run üzerinde yürüsün. İlk tur önce gerçekten `active`, sonra checkpoint'li `halted` olsun; ikinci tur gerçek post-halt native Continue veya izin verilen teknik `resume` yolunu kullansın. Pristine ilk bağlamayı ikinci tur resume başarısı sayma.
- [ ] Sentetik clock ile erken/geç/tekrarlı/değişmiş olay, eksik paused kanıtı, kullanıcı duruşu ve tam iki turu sınayan birim testleri yaz. Canlı 10 saniye denemesi ancak ayrıca açıkça seçildiğinde çalışsın; normal akış bu görevin test modunu açmasın.

## Görev 4 — Gerçek toparlanma ve regresyon kabul matrisi

**Files:** Skill test dizininde `test_candidate_coexistence.py`, `test_goal_binding.py`, `test_watcher_engine.py`, `test_native_usage_source.py`, `test_monitor.py`, `test_watcher_atomic_write.py`; yeni tooling testi.

- [ ] Aynı kaynak ve üretilmiş pakette şu matrisi çalıştır: sağlıklı ilk başlangıç; başarısız ilk başlangıç+taze retry; gerçek biçimli expiry sonrası etkin workflow stop/snapshot/halt/teknik resume; bozuk retry ardından geçerli retry; native paused→Continue→resume; doğal complete. Her satır kendi sonucunu kanıtlasın.
- [ ] Resume entegrasyonunda önce diskten `status == halted` ve verified checkpoint'i gör; resume sonrası aynı goal/run kimliği, yeni izleme dönemi, eski SAVE hash'i, korunmuş kullanıcı yetkisi ve sağlıklı worker'ı doğrula. İlk bağlama veya yalnız start yanıtı bu kabulü karşılamaz.
- [ ] Negatif matris: gerçek quota/early reserve, yeni quota SAVE, açık kullanıcı duruşu, farklı goal, stale/replayed receipt, bozuk checkpoint ve canlı/bilinmeyen child. Her biri yeni iş kabulünü reddetsin. Native `blocked` kararı yalnız native aracın koşullarıyla verilsin.
- [ ] Windows testleri yalnız gerekli sahiplik dosyalarını kopyalasın. Exit kontrolü bilinen süreç kimliği ve terminal health kaydını birlikte beklesin; geçici sharing hatası başarı veya ölüm sayılmasın. Kalıcı izin hatası belirli sürede açık hata vermeli; genel yakala/geç eklenmesin.
- [ ] Odak testleri yeşil olduktan sonra README'deki iki tam komutu çalıştır: `python -X utf8 -B -m unittest discover -s skills/agent-workflow-v1-1/tests` ve `python -X utf8 -B -m unittest discover -s tooling/tests`. Beklenen: sıfır failure/error; yeni test sayısı discovery ile uyuşsun. Önceki 391 test sabit başarı hedefi sayılmasın. Yeni değişiklik/hata olmadan bütün paketi tekrar tekrar çalıştırma.
- [ ] Canlı kabul, gerçek hedefi kullanıcı başlattığında ve başlatıcı hazırken yapılır. Normal başlangıç/izleme/doğal kapanış varsayılan canlı kabul olsun. Expiry veya süreli kesinti testi ayrı açık tanılama kapsamı gerektirir; çalıştırılmadıysa raporda `not_tested` kalsın.

## Görev 5 — Yönerge, güvenli dağıtım ve kapanış kanıtı

**Files:** Skill `SKILL.md`, `references/operations.md`, `references/native-usage.md`, `references/coexistence.md`; mevcut `tooling/build-candidate.py` ve testleri yalnız gerekirse değiştirilir. Kabul çıktısı `reports/agwf-lifecycle-reliability/` altında kişisel/yerel kalır.

- [ ] İlk retry, mevcut halted resume ve kullanıcı/kota duruşunu ayrı karar tablosuyla yaz. Ölçüm/lease yenileme sırası ile geçersiz zaman testinin anlamını güncelle. Arka planda bağımsız native ölçüm varmış gibi ifade kullanma.
- [ ] Güncel çalışma ağacını ve kurulu paketi envanterle; mevcut staged değişiklikleri koru. Canlı worker bulunan kurulumu değiştirme veya durdurma. Eski pin'e bağlı kayıtları belirle; kapalı/aktif olduğuna ilişkin kanıt yoksa sessiz yeniden bağlama/migrasyon yapma, dağıtım engelini somut olarak raporla.
- [ ] Aynı kaynakla iki paket üret; manifest ve ZIP hash'leri eşleşsin. Yeni payload dosyası varsa manifest/package testleri kapsasın. Kurulum eski kuralları gizlice gevşetmesin ve Codex skill/model ayarlarını değiştirmesin.
- [ ] Güvenli güncellemeden sonra kurulu candidate'ı ayrı pasif doğrulama klasöründe çalıştır; payload hash'lerini, doğrudan çalışmayı, sahiplik ve Windows erişimini kontrol et. Bu pasif kontrolü canlı resume kabulü gibi raporlama.
- [ ] Tek sonuç dosyasında gereksinim→kanıt eşlemesi tut: `startup_retry`, `receipt_refresh`, `halted_resume`, `native_continue`, `natural_complete`, `timed_test`, `worker_exit`, `package_match`; her birinin sonucu `passed`, `failed`, `not_tested` veya `blocked_by_external_condition` olsun. Arşivlenmiş eski başarısız kanıtları silme.
- [ ] Tam değişiklik için bağımsız son inceleme yap; özellikle izin aşımı ve rollback/new SAVE korunmasını denetlet. Yalnız seçilmiş değişiklikleri commit et; v1.1 commit ve GitHub push kullanıcı tarafından ayrıca yetkilidir; GitHub Release, otomatik canlı test goal'ı veya eski koşum migrasyonu oluşturma.

## Önerilen yürütme ve bitiş ölçütü

Görev 1 → 2 → 3 → 4 → 5 sırası. Aynı candidate ve test sözleşmeleri paylaşıldığından uygulamayı tek ana ajanın bu oturumda yapması, sonunda bağımsız inceleme alınması önerilir. Görev 3'ün canlı süreli kabulü kullanıcı seçimine bağlıdır; uygulanmaması normal akışın başarısını genişletmez veya daraltmaz, ayrı `not_tested` kalır.

Ürün düzeltmesi ancak ilk başlangıç retry'si ve gerçekten halted durumundan resume matrisi geçtiğinde, mevcut kota/kullanıcı duruşu sınırları korunduğunda, tam testler ve paket denetimi sonuçlandığında tamamlanmış sayılır. Native arayüz sonucu yalnız gerçek araç/durum kanıtıyla doğrulanır. 10 saniye testinin başarı iddiası ayrıca gerçek olay zamanına bağlı canlı zamanlama kanıtı gerektirir.

## Plan öz denetimi

- Gereksinim 1–3: Görev 1 ve 4; gereksinim 4–5: Görev 2; gereksinim 6: Görev 4; gereksinim 7–8: Görev 3; gereksinim 9–10: Görev 4 ve 5.
- Beş Review Focus maddesinin her biri yukarıdaki test adımlarına bağlandı.
- Yeni CLI modları ve fonksiyon imzaları tanımlandı; gerçek host araçlarını çağırma sorumluluğu ana ajanda kaldı.
- Mevcut düzeltmeler regresyonla korunuyor; eski canlı denemeler veya normal kapanış sonucu, henüz çalıştırılmamış toparlanmanın kanıtı sayılmıyor.


## Task 6 — v1.1 sürüm, changelog ve GitHub sunumu

- [ ] Paket/runtime sürümü, teknik kimlik, kurulum yolu ve yeni namespace'i 1.1 yap. Kullanıcının tam 1.1 talebi önceki kimliği koruma kararının yerini alır; eski namespace/pin farklı sürüme sessiz bağlanmayacak.
- [ ] `CHANGELOG.md` içinde v1.1 eklentilerini, düzeltmelerini ve kanıt sınırlarını yaz; gerçek geçmişten doğrulanabilen v1.0 kaydını ekle.
- [ ] README'yi amaç, çözdüğü problemler, kullanım alanları, sınırlar ve kanıtlanmış mimariyle yeniden yaz. Ayrı Mermaid architecture, workflow ve orchestration diyagramları ekle.
- [ ] README'ye temiz checkout'tan manuel kurulum; ajan için kaynak doğrulama/build/harici pin/kurulum/keşif/açık çağırma rehberleri ekle. Hiçbir rehber kendi başına native goal açmasın veya model ayarını değiştirmesin.
- [ ] GitHub description ve ilgili topics'i doğrulanmış yeteneklere göre güncelle. Daha sonraki tam 1.1 talebi kapsamında repo adı ve belge URL'lerini 1.1'e taşı; erişim/branch korumasını değiştirme.
- [ ] README komutlarını geçici temiz dizinde çalıştır; kurulu dosya/pin ve paket sürümü doğrulansın. Üç diyagramın kaynakla tutarlı olduğunu bağımsız incelet.
- [ ] Sadece onaylı kaynak/test/belge değişikliklerini commit et; kişisel rapor, token, kullanım girdisi ve runtime kayıtlarını dışarıda tut. Güncel remote head'i kontrol edip normal fast-forward push kullan.
- [ ] GitHub'daki commit/tree, sürüm/changelog/README blob'ları ve description/topics'i yeniden okuyarak doğrula. Release/tag oluşturulmadığını API'den doğrula.
