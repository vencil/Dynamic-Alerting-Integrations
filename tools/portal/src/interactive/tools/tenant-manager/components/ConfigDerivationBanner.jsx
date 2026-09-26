---
title: "Tenant Manager — ConfigDerivationBanner"
purpose: |
  Non-blocking notice for what the tenant list's 「依設定推算」 state
  could not read (#1988 D1 PR-3): the conf.d files tenant-api skipped
  as unparseable (their tenants have no derived state; a root tenant
  file still appears as a degraded row, #1680), or a load error that
  left every tenant's mode unknown. Fed by derivationNoticeFromSearch; renders nothing when
  the notice is null.
---

const bannerStyle = {
  backgroundColor: 'var(--da-color-warning-soft)',
  border: '1px solid var(--da-color-warning)',
  borderRadius: 'var(--da-radius-md)',
  padding: 'var(--da-space-3) var(--da-space-4)',
  marginBottom: 'var(--da-space-4)',
  fontSize: 'var(--da-font-size-sm)',
  color: 'var(--da-color-fg)',
};

function ConfigDerivationBanner({ notice, t }) {
  if (!notice) return null;
  const { parseFailedFiles, parseFailedHidden, loadError } = notice;
  return (
    <div role="status" aria-live="polite" aria-atomic="true" style={bannerStyle}>
      {loadError && (
        <div>
          {t('無法依設定推算狀態（模式顯示為 unknown）：', 'Could not derive modes from config (shown as unknown): ')}
          <code>{loadError}</code>
        </div>
      )}
      {(parseFailedFiles.length > 0 || parseFailedHidden > 0) && (
        <div>
          {t('以下設定檔解析失敗、已被略過，其租戶無法依設定推算狀態（根目錄的租戶檔會以帶設定錯誤的列出現）：', 'These config files failed to parse and were skipped; their tenants have no derived state (a root tenant file appears as a row with a config error): ')}
          {parseFailedFiles.map((f, i) => (
            <span key={f}>{i > 0 ? ', ' : ''}<code>{f}</code></span>
          ))}
          {parseFailedHidden > 0 && (
            <span>
              {parseFailedFiles.length > 0 ? ' ' : ''}
              {t(`（另有 ${parseFailedHidden} 個不在你的權限範圍內）`, `(${parseFailedHidden} more outside your access scope)`)}
            </span>
          )}
        </div>
      )}
    </div>
  );
}

export { ConfigDerivationBanner };
