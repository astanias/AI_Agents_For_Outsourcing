import type { RouteObject } from "react-router-dom";

import AppLayout from "../layouts/AppLayout";
import Dashboard from "../features/dashboard/Dashboard";
import GroupList from "../features/groups/GroupList";
import CreateGroup from "../features/groups/CreateGroup";
import GroupDetail from "../features/groups/GroupDetail";
import PersonalCalendar from "../features/calendar/PersonalCalendar";
import MeetingList from "../features/meetings/MeetingList";
import ProfileSettings from "../features/settings/ProfileSettings";

export const appRoutes: RouteObject[] = [
  {
    element: <AppLayout />,
    children: [
      { path: "/", element: <Dashboard /> },
      { path: "/groups", element: <GroupList /> },
      { path: "/groups/new", element: <CreateGroup /> },
      { path: "/groups/join", element: <CreateGroup /> },
      { path: "/groups/:groupId", element: <GroupDetail /> },
      { path: "/calendar", element: <PersonalCalendar /> },
      { path: "/meetings", element: <MeetingList /> },
      { path: "/settings", element: <ProfileSettings /> },
    ],
  },
];
