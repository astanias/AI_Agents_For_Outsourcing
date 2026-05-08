import { Routes, Route, Navigate } from "react-router-dom";
import { useAuth } from "./context/AuthContext";
import AppLayout from "./layouts/AppLayout";
import AuthLayout from "./layouts/AuthLayout";
import Login from "./features/auth/Login";
import Signup from "./features/auth/Signup";
import Dashboard from "./features/dashboard/Dashboard";
import GroupList from "./features/groups/GroupList";
import CreateGroup from "./features/groups/CreateGroup";
import GroupDetail from "./features/groups/GroupDetail";
import PersonalCalendar from "./features/calendar/PersonalCalendar";
import MeetingList from "./features/meetings/MeetingList";
import ProfileSettings from "./features/settings/ProfileSettings";

function ProtectedRoute({ children }: { children: React.ReactNode }) {
  const { token, isLoading } = useAuth();
  if (isLoading) {
    return <div style={{ padding: "3rem", textAlign: "center" }}>Restoring your session...</div>;
  }
  return token ? <>{children}</> : <Navigate to="/login" />;
}

export default function App() {
  return (
    <Routes>
      <Route element={<AuthLayout />}>
        <Route path="/login" element={<Login />} />
        <Route path="/signup" element={<Signup />} />
      </Route>
      <Route element={<ProtectedRoute><AppLayout /></ProtectedRoute>}>
        <Route path="/" element={<Dashboard />} />
        <Route path="/groups" element={<GroupList />} />
        <Route path="/groups/new" element={<CreateGroup />} />
        <Route path="/groups/join" element={<CreateGroup />} />
        <Route path="/groups/:groupId" element={<GroupDetail />} />
        <Route path="/calendar" element={<PersonalCalendar />} />
        <Route path="/meetings" element={<MeetingList />} />
        <Route path="/settings" element={<ProfileSettings />} />
      </Route>
    </Routes>
  );
}
