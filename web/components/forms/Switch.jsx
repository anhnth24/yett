import React from 'react';

/** Switch bật/tắt (skills_enabled, subagents_enabled...). Bật = accent. */
export function Switch({ checked = false, onChange, label, disabled = false }) {
  return (
    <label style={{ display: 'inline-flex', alignItems: 'center', gap: 8, cursor: disabled ? 'not-allowed' : 'pointer', fontFamily: 'var(--font-ui)', opacity: disabled ? 0.5 : 1 }}>
      <span
        onClick={disabled ? undefined : () => onChange && onChange(!checked)}
        style={{
          width: 32, height: 18, borderRadius: 999, position: 'relative', flexShrink: 0,
          background: checked ? 'var(--acc)' : 'var(--idle)',
          transition: 'background 150ms ease-out',
        }}
      >
        <span style={{
          position: 'absolute', top: 2, left: checked ? 16 : 2,
          width: 14, height: 14, borderRadius: '50%', background: '#fff',
          boxShadow: '0 1px 2px rgba(20,20,10,0.2)',
          transition: 'left 150ms ease-out',
        }} />
      </span>
      {label ? <span style={{ fontSize: '13px', color: 'var(--body)' }}>{label}</span> : null}
    </label>
  );
}
