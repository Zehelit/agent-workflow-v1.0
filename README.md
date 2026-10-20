# agent-workflow-v1.0

Codex için bağımsız ajan koordinasyonu skill'i. Teknik kimliği `agent-workflow-v1-0` olur ve yalnız açıkça seçildiğinde kullanılır. Limit watcher aynı paketin içindedir; ayrı bir skill veya eski agent-workflow kurulumu gerektirmez.

Python 3.11+ ve standart kütüphane yeterlidir. Kaynak ve paket testleri:

```powershell
python -X utf8 -B -m unittest discover -s skills/agent-workflow-v1-0/tests
python -X utf8 -B -m unittest tooling/tests/test_candidate_package.py
```

Deterministik paketi `python -X utf8 -B tooling/build-candidate.py --output <yeni-boş-klasör>` ile üretin. Kurulum, dış manifest pini ve çalışma alanı bağlama kuralları [paket rehberinde](skills/agent-workflow-v1-0/references/coexistence.md) açıklanır. Kaynak değişiklikleri Codex'te kurulu paketi kendiliğinden güncellemez.

Watcher etkin çalışma sırasında iki kullanım penceresini ölçer. Herhangi birinde kalan pay %20 veya altına düştüğünde yeni iş durur ve kurtarma kaydı hazırlanır. Gerçek pilot ve fayda ölçümü kaynak testlerinden ayrı kabul edilir.

Değerlendirme arşivleri, kişisel koşum kayıtları ve başka projelerin depoları GitHub kaynak paketinin parçası değildir.
