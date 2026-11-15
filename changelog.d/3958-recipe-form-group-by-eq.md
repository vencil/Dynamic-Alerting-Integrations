---
section: Fixed
topic: portal
issues: [3958]
created: 2026-11-15T05:39:24+00:00
---
- **編輯帶 `group_by` 的 threshold recipe 時，表單不再提供 op `==`（portal RecipeBuilder；[#3958](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3958)）**：exporter 不接受 `group_by` 與 `==` 並用，而表單沒有 `group_by` 欄位、編輯時又會把原紀錄的 `group_by` 帶回，所以選 `==` 會送出一筆存檔被拒、表單上又看不出原因的紀錄。現在原紀錄帶非空 `group_by` 時，op 選單不列 `==`，並在下方說明原因與「先在租戶 YAML 拿掉 `group_by`」；原紀錄已經同時帶兩者時，選單把 `==` 顯示成不可選，改成其他 op 後才能送出。
