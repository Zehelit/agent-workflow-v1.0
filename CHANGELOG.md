# Changelog

## 1.1 — 2026-11-10

### Eklendi

- İlk activate / bind-active-goal denemesinin kalıcı kaydı ve haricen korunan hata receipt SHA-256 değeriyle güvenli tekrar.
- Candidate yaşam döngüsü işlemlerinde sahipli tek-yazıcı kilidi.
- Hazırlık, başlatma, gözlem yenileme, post-halt devam ve doğal kapanış için tooling/run-lifecycle-check.py.
- Açık timed-test modu; geç olay sınıflandırması ve ayrı SAVE/checkpoint/halt zamanları. Native pause kanıtı bu araç tarafından üretilmez.
- Durum özetinde gerçek ölçüm zamanı, son tarih, aralık ve erken yenileme önerisi.
- Amaç, mimari, üç diyagram, manuel/ajan kurulum rehberi ve yaşam döngüsü kullanım belgesi.

### Düzeltildi

- Ölçüm eskimesi kaynaklı teknik duruşta aynı yetkili aktif goal altında kontrollü toparlanma; gerçek kota ve kullanıcı duruşları bu yoldan geçmez.
- Başarısız ilk başlangıç/receipt sonrası tekrar sırasında kapsam, SAVE geçmişi, paket bağı ve doğrulanmış worker yokluğu korunur.
- Süreli haftalık pencere istisnası core ve watcher kararlarına aynı biçimde uygulanır; gerçek değerler değiştirilmez.
- Windows'ta tanımlı geçici paylaşım/lock okuma hataları için sınırlı tekrar; kalıcı ve sınıflandırılamayan hatalar görünür kalır.
- Ölçüm yenileme sırası, lease ve post-halt fresh receipt gereksinimleri açıklığa kavuşturuldu.
- Eski test yardımcılarının fonksiyon imzası ve süreç/dosya bekleme davranışları güncellendi.

### Uyumluluk ve kanıt sınırları

- Paket/görünen sürüm 1.1; teknik skill kimliği ve run namespace'i agent-workflow-v1-0 olarak korunur.
- Paket güncellemesi eski koşumun immutable bağını değiştirmez; aktif kurulumu yerinde düzenlemeyin.
- Python 3.11+ ve standart kütüphane; ikinci Codex sunucusu veya bağımsız native usage erişimi yok.
- İzole testler sentetik host gözlemleri ve gerçek Python worker süreçleri kullanır. Canlı Remote/Continue ve arayüz zamanlama kabulü ayrıca sınanmalıdır.
- Bu sürüm için GitHub Release veya tag oluşturulmaz; sürüm kaynak commit'iyle kaydedilir.

## 1.0 — önceki kaynak sürümü

- Sabitlenmiş candidate girişi, typed workflow, rol belgeleri, dosya sahipliği ve kapasite kontrolleri.
- Entegre modelsiz watcher, checkpoint/restore, kalıcı koordinasyon günlüğü ve deterministik paketleme.
- Bu kayıt geçmiş bir GitHub Release veya tag bulunduğu iddiası değildir.
