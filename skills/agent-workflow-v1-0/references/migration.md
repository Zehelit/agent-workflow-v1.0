# Durum uyumluluğu ve kurtarma sınırı

v1.0 çalışma zamanı yalnızca onaylı, değişmez çalıştırma alanındaki güncel schema2 durumunu kabul eder. Bilinmeyen, bozuk ve eski durumlar operasyonel durum değiştirilmeden reddedilir. Eski yetki metinleri, arşivlenmiş görev makbuzları ve güncelliğini yitirmiş hedef gözlemleri yürütme yetkisi vermez.

Sabitlenmiş `scripts/candidate.py` girişi taşıma komutu sunmaz; çekirdek kaynakta da eski durum dönüştürücüsü veya eski yetki doğrulama dalı bulunmaz. Başka bir kurulumdan otomatik içe aktarma, bootstrap veya paket geri dönüşü yapılmaz. Onaylı paket güncellemesinden sonra yeni bir çalıştırma oluşturun; mevcut çalıştırmanın manifestini veya yardımcı eşlemesini değiştirerek doğrulamayı aşmayın.

Güncel kurtarma, kabul edilmiş çalıştırmanın doğrulanmış kontrol noktasını ve yeni, geçerli kullanıcı yetkisini kullanır. Kaynak zaman damgaları, tekrar kullanım rezervasyonları, dosya bütünlüğü ve yazıcıların durduğu gözlemi korunur. Eski durdurma veya devam metnini yeni talimat olarak kullanmayın; kurtarılan hedefi otomatik etkinleştirmeyin.

Kontrol noktalarının sahibi `agent-workflow-v1-0` kimliğidir. Çekirdek nesnenin bağımsız varsayılan durum dizini `.agent-workflow-v1-0`; desteklenen sabitlenmiş girişin çalıştırma dizini `.agent-workflow-v1-0/runs/<run-id>` olur. Eski `.agent-workflow` dizini içe aktarılmaz ve güncel kontrol noktası temizliği eski sahiplik işaretlerini silmez. Doğrudan çekirdek komut satırı kullanımı reddedilir.
