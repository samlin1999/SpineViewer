"""執行緒安全的記憶化（沿用 csdview 的同名模組與規則）。

貼圖快取與 Renderer 都會被多執行緒同時使用：
- 批次指令（frames／gif／sheet）用執行緒池平行渲染
- 網頁伺服器每個請求一個執行緒，且共用同一個 Renderer

csdview 曾經因為快取沒同步、又用 `id()` 當快取鍵，在 8 執行緒下 12 次有 7 次畫出別張圖。
所以這裡的快取全部走同一個函式，規則也照搬：

1. **鍵必須穩定**——用檔案路徑加 mtime 之類的內容識別，絕對不要用 `id()`。
2. **值必須建立後就不再修改**——快取回傳的是共用物件，任何一方改動都會影響其他人。
   貼圖陣列載入後會設成唯讀（`setflags(write=False)`），改到就直接報錯。
"""
from __future__ import annotations

import threading
from typing import Any, Callable

_MISSING = object()


def memoize(cache: dict, key: Any, lock: threading.Lock,
            build: Callable[[], Any], limit: int = 0) -> Any:
    """取快取；沒有就建立。

    命中時不進鎖（絕大多數情況），未命中才鎖起來並重新確認一次——
    否則兩個執行緒會同時建立，其中一份被丟棄，浪費之外也可能讓呼叫端
    拿到兩個不同的物件。

    `build()` 回傳 None 也會被記住（用來記「這個資源找不到」），
    所以用哨兵值判斷是否命中，不能只看 `is not None`。

    `limit > 0` 時，寫入後若超過上限就整份清掉再只留這一筆（防止無界成長，不是 LRU）。
    """
    hit = cache.get(key, _MISSING)
    if hit is not _MISSING:
        return hit
    with lock:
        hit = cache.get(key, _MISSING)
        if hit is not _MISSING:
            return hit
        value = build()
        cache[key] = value
        if limit and len(cache) > limit:
            cache.clear()
            cache[key] = value
        return value
