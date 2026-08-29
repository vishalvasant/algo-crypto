import {
  Bell,
  BookOpen,
  Briefcase,
  LayoutDashboard,
  LineChart,
  LogOut,
  ScrollText,
  User,
  Bot,
} from "lucide-react";
import { NavLink } from "react-router-dom";
import type { LucideIcon } from "lucide-react";

type NavItem = {
  to: string;
  label: string;
  icon: LucideIcon;
  end?: boolean;
  badge?: boolean;
};

const NAV_ITEMS: NavItem[] = [
  { to: "/", label: "Dashboard", icon: LayoutDashboard, end: true },
  { to: "/holdings", label: "Positions", icon: Briefcase },
  { to: "/order-book", label: "Orders", icon: BookOpen },
  { to: "/trades", label: "Analytics", icon: LineChart },
  { to: "/notifications", label: "Alerts", icon: Bell, badge: true },
  { to: "/logs", label: "Logs", icon: ScrollText },
];

export function RefSidebarNav({ alertCount = 0 }: { alertCount?: number }) {
  return (
    <nav className="ref-sidebar-nav">
      {NAV_ITEMS.map((item) => {
        const { label, icon: Icon, to } = item;
        const showBadge = item.badge && alertCount > 0;
        return (
          <NavLink
            key={to}
            to={to}
            end={item.end}
            className="ref-nav-link"
          >
            <Icon size={16} strokeWidth={1.75} />
            <span>{label}</span>
            {showBadge ? <span className="nav-badge">{alertCount}</span> : null}
          </NavLink>
        );
      })}
    </nav>
  );
}

export function RefSidebarFooter({
  status,
  username,
  brokerOn,
  onLogout,
}: {
  status?: string;
  username?: string;
  brokerOn?: boolean;
  onLogout: () => void;
}) {
  return (
    <div className="ref-sidebar-footer">
      <div className="ref-sidebar-status">
        <Bot size={13} />
        <span>{status ?? "—"}</span>
        {brokerOn ? <span className="sidebar-live-dot" /> : null}
      </div>
      <div className="ref-sidebar-user">
        <User size={12} />
        <span>{username}</span>
        <button type="button" className="btn btn-ghost btn-sm" onClick={onLogout} title="Logout">
          <LogOut size={14} />
        </button>
      </div>
    </div>
  );
}
