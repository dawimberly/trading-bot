for y in $(seq 2006 2026); do for q in 1 2 3 4; do f=raw/${y}q${q}_form345.zip; [ -s $f ] && continue
curl -sf -A "StockBot research contact@example.com" -o $f https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets/${y}q${q}_form345.zip || rm -f $f; sleep 0.5; done; done; ls raw | wc -l
