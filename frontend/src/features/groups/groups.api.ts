import { apiJson } from "../../lib/api";

export interface CreateGroupPayload {
  name: string;
  description?: string;
}

export interface JoinGroupPayload {
  inviteCode?: string;
  groupId?: number;
}

export interface GroupSummary {
  id: number;
  name: string;
  description: string | null;
  role: "owner" | "admin" | "member" | string;
}

export interface GroupMember {
  id: number;
  email: string;
  first_name: string;
  last_name: string;
  role: "owner" | "admin" | "member" | string;
}

export interface GroupAvailabilitySlot {
  id: number;
  user_id: number;
  email: string;
  first_name: string;
  last_name: string;
  day_of_week: number;
  start_time: string;
  end_time: string;
}

export async function getGroups() {
  return apiJson<GroupSummary[]>("/api/groups/");
}

export async function createGroup(payload: CreateGroupPayload) {
  return apiJson<GroupSummary>("/api/groups/", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function joinGroup(payload: JoinGroupPayload) {
  return apiJson<GroupSummary>("/api/groups/join", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function getGroup(groupId: number) {
  return apiJson<GroupSummary>(`/api/groups/${groupId}`);
}

export async function getGroupMembers(groupId: number) {
  return apiJson<GroupMember[]>(`/api/groups/${groupId}/members`);
}

export async function getGroupAvailability(groupId: number) {
  return apiJson<GroupAvailabilitySlot[]>(`/api/groups/${groupId}/availability`);
}
