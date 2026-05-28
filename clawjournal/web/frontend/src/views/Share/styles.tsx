import { colors } from '../../theme.ts';

export const SHARE_SHELL_WIDTH = '1120px';

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
    @keyframes clawConfetti { 0% { transform: translate(0,0) rotate(0); opacity: 1; } 100% { transform: translate(var(--cdx), var(--cdy)) rotate(var(--cr)); opacity: 0; } }
    @keyframes clawPkgDrop { 0% { opacity: 0; transform: translate(-50%,-30px) scale(.85); } 15% { opacity: 1; } 70% { opacity: 1; transform: translate(-50%, 80px) scale(.85); } 100% { opacity: 0; transform: translate(-50%, 120px) scale(.3); } }
    @keyframes clawThump { 0% { transform: scale(1); } 40% { transform: scale(1.02) translateY(2px); } 100% { transform: scale(1); } }
  `}</style>
);
