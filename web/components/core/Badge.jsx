import React from 'react';

/** Trạng thái = dot màu 6px + chữ trung tính. Không badge nền màu, không màu đứng một mình. */
export function Badge({ status = 'idle', pulse = false, children }) {
  const dots = {
    running: 'var(--acc)',
    ok: 'var(--acc)',
    warn: 'var(--warn)',
    danger: 'var(--danger)',
    idle: 'var(--idle)',
  };
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontFamily: 'var(--font-ui)', fontSize: '11.5px', fontWeight: 500, color: 'var(--mut)' }}>
      <span style={{ width: 6, height: 6, borderRadius: '50%', background: dots[status] || dots.idle, animation: pulse ? 'yettPulse 1.6s ease-in-out infinite' : 'none' }} />
      <style>{'@keyframes yettPulse{0%,100%{opacity:1}50%{opacity:.35}}'}</style>
      {children}
    </span>
  );
}
