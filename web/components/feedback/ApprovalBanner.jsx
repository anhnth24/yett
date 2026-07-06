import React from 'react';
import { Button } from '../core/Button.jsx';

/** Banner duyệt lệnh — tương tác trung tâm của yett. Viền warn, dot pulse, nguyên văn lệnh bằng mono. */
export function ApprovalBanner({ tool, host, command, timeoutNote = 'timeout 300s → deny', onApprove, onDeny, decided = null }) {
  if (decided === 'ok') {
    return (
      <div style={{ padding: '11px 16px', borderRadius: 'var(--r-card)', background: 'var(--card)', border: '1px solid var(--line)', boxShadow: 'var(--shadow-card)', fontFamily: 'var(--font-ui)', fontSize: '12.5px', color: 'var(--sub)' }}>
        ✓ Đã duyệt {tool} — lệnh chạy trong sandbox, kết quả vào trace.
      </div>
    );
  }
  if (decided === 'no') {
    return (
      <div style={{ padding: '11px 16px', borderRadius: 'var(--r-card)', background: 'var(--card)', border: '1px solid var(--line)', boxShadow: 'var(--shadow-card)', fontFamily: 'var(--font-ui)', fontSize: '12.5px', color: 'var(--sub)' }}>
        ✕ Đã từ chối {tool} — turn tiếp tục, agent nhận kết quả deny.
      </div>
    );
  }
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 14, padding: '12px 16px', borderRadius: 'var(--r-card)', background: 'var(--card)', border: '1px solid var(--warn-line)', boxShadow: 'var(--shadow-card)', fontFamily: 'var(--font-ui)' }}>
      <span style={{ width: 8, height: 8, borderRadius: '50%', background: 'var(--warn)', animation: 'yettPulse 1.6s ease-in-out infinite', flexShrink: 0 }} />
      <style>{'@keyframes yettPulse{0%,100%{opacity:1}50%{opacity:.35}}'}</style>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: '13px', fontWeight: 600, color: 'var(--ink)' }}>Cần bạn duyệt — {tool}{host ? ' trên ' + host : ''}</div>
        <div style={{ fontFamily: 'var(--font-mono)', fontSize: '12px', color: 'var(--mut)', marginTop: 2, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{command}</div>
      </div>
      <div style={{ display: 'flex', gap: 8, flexShrink: 0 }}>
        <Button variant="primary" onClick={onApprove}>Duyệt</Button>
        <Button variant="secondary" onClick={onDeny}>Từ chối</Button>
      </div>
      <div style={{ fontSize: '11px', color: 'var(--faint)', flexShrink: 0 }}>{timeoutNote}</div>
    </div>
  );
}
