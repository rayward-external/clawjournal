import { colors } from '../../theme.ts';

export const SHARE_SHELL_WIDTH = '1360px';

export const btnPrimary = {
  display: 'inline-flex', alignItems: 'center', gap: 8,
  padding: '9px 18px', background: colors.gray900, color: colors.white,
  border: 'none', borderRadius: 8, fontSize: 13, fontWeight: 500,
  cursor: 'pointer', whiteSpace: 'nowrap' as const,
};

export const btnSecondary = {
  display: 'inline-flex', alignItems: 'center', gap: 8,
  padding: '8px 14px', background: colors.white, color: colors.gray700,
  border: `1px solid ${colors.gray300}`, borderRadius: 8, fontSize: 13, fontWeight: 500,
  cursor: 'pointer', whiteSpace: 'nowrap' as const,
};

export const btnGhost = {
  display: 'inline-flex', alignItems: 'center', gap: 6,
  padding: '5px 10px', background: 'transparent', color: colors.gray600,
  border: 'none', borderRadius: 6, fontSize: 12, fontWeight: 500,
  cursor: 'pointer',
};

export const globalStyles = (
  <style>{`
    @keyframes clawSpin { to { transform: rotate(360deg); } }
    @keyframes clawFadeIn { from { opacity: 0 } to { opacity: 1 } }
    @keyframes clawChipPop { from { opacity: 0; transform: translateY(4px) scale(.85); } to { opacity: 1; transform: translateY(0) scale(1); } }
    @keyframes clawRingOut { to { transform: scale(2); opacity: 0; } }
    @keyframes clawCheckIn { from { transform: scale(0); opacity: 0; } to { transform: scale(1); opacity: 1; } }
    @keyframes clawSuccessFlash {
      0% { opacity: 0; transform: translateY(-100px) scale(.72); }
      16% { opacity: 1; }
      58% { opacity: .45; transform: translateY(40px) scale(1); }
      100% { opacity: 0; transform: translateY(120px) scale(1.08); }
    }
    @keyframes clawConfettiDrop {
      0% { transform: translate3d(0,-36px,0) rotate(0) scale(.7); opacity: 0; }
      8% { opacity: 1; }
      76% { opacity: 1; }
      100% { transform: translate3d(var(--cdx),var(--cdy),0) rotate(var(--cr)) scale(1); opacity: 0; }
    }
    .claw-success-flash {
      animation: clawSuccessFlash 1500ms cubic-bezier(.16,.72,.24,1) forwards;
      animation-delay: var(--flash-delay, 0ms);
    }
    .claw-success-confetti span {
      animation: clawConfettiDrop var(--cduration) cubic-bezier(.18,.68,.28,1) both;
      animation-delay: var(--cdelay);
      will-change: transform, opacity;
    }
    @keyframes clawPkgDrop { 0% { opacity: 0; transform: translate(-50%,-30px) scale(.85); } 15% { opacity: 1; } 70% { opacity: 1; transform: translate(-50%, 80px) scale(.85); } 100% { opacity: 0; transform: translate(-50%, 120px) scale(.3); } }
    @keyframes clawThump { 0% { transform: scale(1); } 40% { transform: scale(1.02) translateY(2px); } 100% { transform: scale(1); } }
    @media (prefers-reduced-motion: reduce) {
      .claw-success-flash { display: none; }
      .claw-success-confetti .claw-confetti-later { display: none; }
      .claw-success-confetti span {
        animation: none !important;
        opacity: .72 !important;
        transform: translate3d(0,var(--cstatic-y),0) rotate(var(--cr));
      }
    }
  `}</style>
);
