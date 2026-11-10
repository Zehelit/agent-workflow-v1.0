# Tekrarlanabilir yaşam döngüsü kontrolü

`tooling/run-lifecycle-check.py`, host araçlarının yerine geçmez. Ana ajan gerçek goal, kullanım ve alt ajan envanterini alır; araç bunları sabitlenmiş candidate üzerinden işler. Bir oturumun tek yazarı olmalıdır. Oturum ve girdi dosyalarını Git dışında, özel bir dizinde tutun.

## Hazırlık

Önce paketi üretip harici manifest pin'ini koruyun. `prepare` dosya bütünlüğünü kontrol eder ve yeni oturum dosyası yazar; candidate veya worker çalıştırmaz. Hazırlığı gerçek ölçümden önce bitirin.

```powershell
python -X utf8 -B tooling/run-lifecycle-check.py prepare --candidate C:/build/agent-workflow-v1-1/scripts/candidate.py --manifest C:/build/agent-workflow-v1-1/manifest.json --manifest-sha256 PRESERVED_SHA256 --version 1.1 --project C:/project --run-id check-1 --session C:/private/check-1.json
```

`PRESERVED_SHA256` kabul edilen build receipt'indeki gerçek değerdir. Proje dizini mevcut, oturum dosyası yeni olmalıdır. Kaynağı veya paketi değiştirdikten sonra aynı oturumu farklı pin'e yeniden bağlamayın.

## Host girdi nesnesi

`run`, `observe`, `resume`, `close` ve `timed-test` komutları `--input` ile JSON nesnesi alır:

| Alan | İçerik |
|---|---|
| `activation` | Yalnız ilk `run` için, candidate `bind-active-goal` komutunun tam typed payload'u. `native_binding`, eşleşen `app_goal_observation`, objective, yorumlama/procedural kontrol, gerçekten gözlenmiş model/effort listesi, slot limiti, gerçek inventory ve gözlem zamanı. |
| `app_goal_observation` | Gerçek `get_goal` gözlemi: goal/thread kimliği, objective, goal oluşturma zamanı, status, `source_kind: get_goal`, gerçek kaynak referansı ve `observed_at`; varsa gerçek `updated_at`. |
| `usage` | [Native receipt sözleşmesindeki](../skills/agent-workflow-v1-1/references/native-usage.md) `source`, `sourceId`, `observedAt`, `result`. Yalnız gerçek gerekli pencereleri saklayın. |
| `inventory` | Gerçek güncel alt ajan kimlik/durum listesi. Gerçekten alt ajan yoksa `[]`. |
| `checkpoint_files` | Ana ajanın yazdığı dahil kurtarılacak tam proje-relative dosyalar; silinen yolları da ekleyin. Task claim'leri core tarafından ayrıca kapsanır. |
| `writers_quiescent` | Snapshot öncesinde bütün yazıcıların durduğunu host gerçekten doğruladıysa `true`. Araç bu gözlemi kendi başına üretemez. |
| `next_action` | Kurtarma veya doğal kapanış sonrası anlamlı sonraki adım. |
| `weekly_exception` | İsteğe bağlı, yalnız ilk `run`: kullanıcının gerçek `exact_quote` değeri ve sınırlı `valid_until` Unix zamanı. Varsayılan bir istisna yoktur. |
| `resume` | İsteğe bağlı, tam candidate resume payload'u. Yoksa yalnız kaydedilmiş ölçüm eskimesi için `usage_recovery: true` denenir. Gerçek kota/kullanıcı duruşları bundan yetki kazanmaz. |

Native API ayrı goal kimliği vermiyorsa `threadId:createdAt` gibi türetilmiş bir kimlik kullanılabilir; bunun sağlayıcının verdiği kimlik olduğu iddia edilmez. Bütün çağrılarda aynı türetme ve gerçek kaynak zamanları korunmalıdır. Typed alanların ayrıntısı [operations](../skills/agent-workflow-v1-1/references/operations.md) ve [commands](../skills/agent-workflow-v1-1/references/commands.md) belgelerindedir.

## Normal akış

```powershell
python -X utf8 -B tooling/run-lifecycle-check.py run --session C:/private/check-1.json --input C:/private/initial-observation.json
python -X utf8 -B tooling/run-lifecycle-check.py observe --session C:/private/check-1.json --input C:/private/new-observation.json
python -X utf8 -B tooling/run-lifecycle-check.py close --session C:/private/check-1.json --input C:/private/final-observation.json
```

Ana ajan her gözlemi gerçek araç çağrısından hemen sonra kaydeder; dosya yazma zamanı ölçüm zamanı değildir. Araç `status.at == usage.observedAt`, canlı sahiplik, epoch, heartbeat, sample ve lease koşullarını birlikte doğrular. `observe` lease'i ayrıca yeniler. `refresh_by` gerçek karar son tarihini değiştirmeyen erken yenileme önerisidir.

`CHECKPOINT_SOON` anında ana ajan tamamlanmış işi kaydeder; `checkpoint_files` kapsamını doğru tutar. Doğal `close`, taze izleme, doğrulanmış snapshot, complete ve gerçek worker yokluğunu gerektirir. Normal mod yapay SAVE üretmez.

İlk `run` girdisinde `weekly_exception` varsa araç önce izleyicide süreli politikayı kurar ve `NEED_POLICY_OBSERVATION` döndürür. Workflow henüz etkinleşmez. Politikadan sonra yeni gerçek kullanım/goal gözlemi alıp aynı oturumda `run` çağrısını tekrar yapın; eski receipt'in zamanını değiştirmeyin.

## Teknik duruş ve devam

İzleyici SAVE istemişse `observe` stop → snapshot → halt yolunu uygular ve `NEED_POST_HALT_OBSERVATION` döndürür. Bundan sonra **yeni** goal, kullanım ve inventory gözlemi alın. Önceden alınmış girdiyi tekrar kullanmayın:

```powershell
python -X utf8 -B tooling/run-lifecycle-check.py resume --session C:/private/check-1.json --input C:/private/after-halt-observation.json
```

Core duruş nedenini kendi watcher kaydından denetler. Aynı aktif hedef, değişmemiş checkpoint ve dosyalar, duruştan sonra alınmış fresh gözlemler ve durmuş sahipli worker gereklidir. Gerçek kota/erken rezerv ve kullanıcı duruşları ayrı gerçek devam yetkisi ister. Araç native goal'u paused/blocked/active yapmaz.

İlk başlangıç başarısızsa candidate'ın verdiği başlangıç denemesi hash'i oturum dosyasına saklanır. Yeni gözlemle aynı `run` komutu tekrar kullanılabilir. Oturum dosyası harici kanıt kaydının parçasıdır; değiştirilmiş dosyayı yeniden onaylanmış saymayın. Tamamlanmış veya belirsiz deneme otomatik yeniden başlatılmaz.

## Açık süreli tanılama

Yalnız ayrıca seçilmiş testte `timed-test` kullanın. Worker olaydan önce hazırlanmış ve sağlıklı olmalıdır. Event JSON alanları: `event_id`, `goal_id`, `thread_id`, `source_id`, `event_at`, `observed_at`. Kaynak ve zamanlar gerçek native olaya dayanır.

```powershell
python -X utf8 -B tooling/run-lifecycle-check.py timed-test --session C:/private/check-1.json --input C:/private/current-observation.json --event-input C:/private/native-event.json --delay-seconds 10 --tolerance-seconds 1
```

Geç gelen olay `INVALID_TIMING` olur; bunu düzeltmek için derhal durdurup başarılı saymaz. Süre içindeki sonuç `SAVE_REQUEST_ON_TIME` yalnız SAVE isteğinin zamanını gösterir. Native pause zamanı/başarısı ayrı host gözlemidir; araç `native_pause_verified: false` ve `native_paused_at: null` bırakır. Tam canlı iki tur kabulünde gerçekten aktif → halted → gerçek devam → aktif sırası ve native durum kanıtı ayrıca tutulur. Sentetik test saatiyle geçen test, canlı arayüz kabulü değildir.
