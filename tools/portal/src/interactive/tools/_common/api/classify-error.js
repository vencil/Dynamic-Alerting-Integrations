// tenant-api error classification (#3202).
//
// tenant-api gives every error a `code`, and each code carries exactly one
// remedy — what a client can do about it (refresh and retry, wait and retry,
// fix the request, fix the config in git, pick another id, ...). The table of
// codes and remedies is generated from the Go source into
// ../data/tenant-api-errors.json (`make error-contract`, CI checks drift), so
// the portal reads the remedy from there instead of keeping its own copy of
// "which 409 means what".
//
// The rule that matters: an answer whose code is missing or not in the
// contract is classified `unknown` and gets no remedy's copy — the server's
// message when there is one, a generic message otherwise. It is never
// guessed into `refresh_retry` from its status — a 409 is not necessarily a
// base_hash race, and "refresh and retry" on a fix-in-git 409 sends the user
// round a loop that cannot succeed (#3202).
import ERROR_CONTRACT from '../data/tenant-api-errors.json';

const CONTRACT_BY_CODE = new Map(ERROR_CONTRACT.codes.map((c) => [c.code, c]));

// Every code in the contract, as a constant: compare `body.code` against
// ERROR_CODES.<CODE> rather than a string literal, so a code that leaves the
// contract shows up as `undefined` here and in the drift test, not as a
// comparison that silently never matches.
const ERROR_CODES = Object.freeze(Object.fromEntries(ERROR_CONTRACT.codes.map((c) => [c.code, c.code])));

// The remedy for an answer whose code is missing or unknown.
const UNKNOWN_REMEDY = 'unknown';

// One message per remedy (plus `unknown`). The Vitest contract test keeps the
// keys equal to the contract's remedy set plus `unknown`: a new remedy in the
// contract without a message here fails the test, and so does a message for a
// remedy the contract no longer has.
const REMEDY_COPY = Object.freeze({
  refresh_retry: {
    zh: '配置已被其他人更新，請重新整理頁面後再試。',
    en: 'Configuration was updated by someone else. Please refresh the page and try again.',
  },
  retry_after: {
    zh: '服務暫時忙碌或無法使用，請稍候再送出同樣的請求。',
    en: 'The service is temporarily busy or unavailable. Wait, then send the same request again.',
  },
  fix_request: {
    zh: '請求被拒：內容需要修正，原樣重送不會成功。',
    en: 'Request refused: it needs to be corrected; sending it again unchanged will not help.',
  },
  fix_in_repo: {
    zh: '寫入被拒：設定庫裡的設定需要先在 git 修正，重整重試不會成功。',
    en: 'Write refused: the configuration in the config repository has to be fixed in git first; refreshing and retrying will not help.',
  },
  choose_other: {
    zh: '這個名稱或 ID 無法使用，請改用其他的。',
    en: 'This name or ID is not available. Choose a different one.',
  },
  permission: {
    zh: '權限不足或尚未登入，無法執行此操作。',
    en: 'You lack the permission (or sign-in) this operation needs.',
  },
  operator: {
    zh: '伺服器端的設定或狀態有問題，重試不會成功，請聯絡平台管理者。',
    en: 'A problem on the server side prevents this; retrying will not help. Contact the platform operator.',
  },
  not_found: {
    zh: '找不到要操作的對象，可能已被刪除，請重新整理確認。',
    en: 'What this request names does not exist; it may have been removed. Refresh to check.',
  },
  existing: {
    zh: '已有既存的項目擋住這個操作（例如待審的 PR／MR），請先處理那一個。',
    en: 'Something that already exists stands in the way (for example an open PR/MR). Act on that one first.',
  },
  refresh: {
    zh: '寫入可能已經生效，請先重新整理確認目前狀態，不要直接重送。',
    en: 'The write may already have taken effect. Refresh and check the current state before doing anything; do not resend blindly.',
  },
  [UNKNOWN_REMEDY]: {
    zh: '操作失敗。',
    en: 'Operation failed.',
  },
});

// Retry-After as delta-seconds; an HTTP-date or garbage gives null.
function parseRetryAfterHeader(value) {
  if (value === null || value === undefined) return null;
  const s = String(value).trim();
  if (!/^\d+$/.test(s)) return null;
  return parseInt(s, 10);
}

/**
 * Classifies a tenant-api error answer.
 *
 * @param {number} status       HTTP status of the answer.
 * @param {object} body         Parsed JSON body (may be null / non-object).
 * @param {object} [opts]
 * @param {string} [opts.retryAfter]  The Retry-After header, when the caller has it.
 * @returns {{remedy: string, retryAfterS: (number|null), known: boolean, code: (string|null)}}
 *   remedy is one of the contract's remedies, or 'unknown' when the code is
 *   missing or not in the contract. retryAfterS: body.retry_after_s, else the
 *   Retry-After header, else the contract's default for the code, else null.
 */
function classifyApiError(status, body, opts) {
  const b = body && typeof body === 'object' ? body : {};
  const code = typeof b.code === 'string' && b.code ? b.code : null;
  const entry = code ? CONTRACT_BY_CODE.get(code) : undefined;

  let retryAfterS = null;
  if (typeof b.retry_after_s === 'number' && b.retry_after_s >= 0) {
    retryAfterS = b.retry_after_s;
  } else {
    retryAfterS = parseRetryAfterHeader(opts && opts.retryAfter);
    if (retryAfterS === null && entry && typeof entry.retry_after_s === 'number') {
      retryAfterS = entry.retry_after_s;
    }
  }

  if (!entry) {
    return { remedy: UNKNOWN_REMEDY, retryAfterS, known: false, code };
  }
  return { remedy: entry.remedy, retryAfterS, known: true, code };
}

function tr(pair) {
  const t = (typeof window !== 'undefined' && window.__t) || ((zh, en) => en);
  return t(pair.zh, pair.en);
}

/** The UI message for a remedy (the `unknown` message for anything else). */
function remedyMessage(remedy) {
  return tr(REMEDY_COPY[remedy] || REMEDY_COPY[UNKNOWN_REMEDY]);
}

/**
 * The message to show for a classified answer: the remedy's message, the
 * wait when there is one, and the server's own text when it adds anything.
 * `refresh_retry` leaves the server text out (it says the same thing);
 * `unknown` shows the server text alone when there is one.
 */
function apiErrorMessage(classified, serverError) {
  const extra = typeof serverError === 'string' ? serverError.trim() : '';
  if (classified.remedy === UNKNOWN_REMEDY) {
    return extra || remedyMessage(UNKNOWN_REMEDY);
  }
  let msg = remedyMessage(classified.remedy);
  if (classified.remedy === 'retry_after' && typeof classified.retryAfterS === 'number') {
    msg += ' ' + tr({ zh: `（約 ${classified.retryAfterS} 秒後）`, en: `(about ${classified.retryAfterS}s)` });
  }
  if (extra && classified.remedy !== 'refresh_retry') msg += ' ' + extra;
  return msg;
}

export {
  ERROR_CODES,
  REMEDY_COPY,
  UNKNOWN_REMEDY,
  classifyApiError,
  remedyMessage,
  apiErrorMessage,
};
