import { useEffect, useState } from 'react';
import { FiMoreVertical } from 'react-icons/fi';
import { useNavigate } from 'react-router-dom';
import { getGroups } from './groups.api';

interface Group {
  id: number;
  name: string;
  description: string | null;
  role: string;
}

export default function GroupList() {
  const [openMenuGroupId, setOpenMenuGroupId] = useState<number | null>(null);
  const [copiedGroupId, setCopiedGroupId] = useState<number | null>(null);
  const [groups, setGroups] = useState<Group[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState('');
  const navigate = useNavigate();

  useEffect(() => {
    const fetchGroups = async () => {
      try {
        const data = await getGroups();
        setGroups(data);
      } catch (err) {
        const message = err instanceof Error ? err.message : 'Could not connect to the backend server.';
        if (message.toLowerCase().includes('unauthorized') || message.toLowerCase().includes('not authenticated')) {
          localStorage.removeItem('access_token');
          navigate('/login');
          return;
        }
        setError(message);
      } finally {
        setIsLoading(false);
      }
    };

    fetchGroups();
  }, [navigate]);

  if (isLoading) return <div className="p-8 text-slate-400">Loading your teams...</div>;
  if (error) return <div className="p-8 text-red-400">{error}</div>;

  return (
    <div className="max-w-4xl mx-auto">
      <div className="flex justify-between items-center mb-8">
        <h1 className="text-3xl font-bold text-slate-800 dark:text-white">Your Groups</h1>
        <div className="flex items-center gap-3">
          <button
            onClick={() => navigate('/groups/new')}
            className="bg-blue-600 hover:bg-blue-500 text-white px-4 py-2 rounded-lg font-medium transition"
          >
            + Create Group
          </button>
          <button
            onClick={() => navigate('/groups/join')}
            className="bg-emerald-600 hover:bg-emerald-500 text-white px-4 py-2 rounded-lg font-medium transition"
          >
            Join Group
          </button>
        </div>
      </div>
      
      {groups.length === 0 ? (
        <div className="bg-slate-100 dark:bg-slate-800 rounded-xl p-8 text-center border border-slate-200 dark:border-slate-700">
          <p className="text-slate-500 dark:text-slate-400">You are not a member of any groups yet.</p>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
          {groups.map((group) => (
            <div key={group.id} className="relative bg-white dark:bg-slate-800 p-6 rounded-xl border border-slate-200 dark:border-slate-700 shadow-sm hover:shadow-md transition">
              <div className="flex justify-between items-start mb-4">
                <h3 className="text-xl font-semibold text-slate-800 dark:text-white">{group.name}</h3>
                <div className="flex items-center gap-2">
                  <span className="px-2.5 py-1 text-xs font-medium bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-300 rounded-full capitalize">
                    {group.role}
                  </span>
                  <div className="relative">
                    <button
                      type="button"
                      onClick={() => setOpenMenuGroupId((current) => (current === group.id ? null : group.id))}
                      className="rounded-full p-2 text-slate-500 hover:bg-slate-100 hover:text-slate-800 dark:text-slate-300 dark:hover:bg-slate-700 dark:hover:text-white transition"
                      aria-label={`Show join token for ${group.name}`}
                    >
                      <FiMoreVertical className="h-5 w-5" />
                    </button>

                    {openMenuGroupId === group.id && (
                      <div className="absolute right-0 top-11 z-20 w-64 rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 p-4 shadow-lg">
                        <p className="text-xs font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400 mb-2">
                          Group Token
                        </p>
                        <div className="rounded-lg bg-slate-50 dark:bg-slate-800 border border-slate-200 dark:border-slate-700 px-3 py-2 font-mono text-sm tracking-[0.3em] text-slate-800 dark:text-white break-all">
                          {formatGroupToken(group.id)}
                        </div>
                        <p className="mt-2 text-xs text-slate-500 dark:text-slate-400">
                          Share this 9-digit token to let someone join the group.
                        </p>
                        <button
                          type="button"
                          onClick={async () => {
                            if (navigator.clipboard?.writeText) {
                              await navigator.clipboard.writeText(formatGroupToken(group.id));
                            }
                            setCopiedGroupId(group.id);
                            window.setTimeout(() => {
                              setCopiedGroupId((current) => (current === group.id ? null : current));
                            }, 1200);
                          }}
                          className="mt-3 w-full rounded-lg bg-blue-600 px-3 py-2 text-sm font-medium text-white transition hover:bg-blue-500"
                        >
                          {copiedGroupId === group.id ? 'Copied!' : 'Copy Token'}
                        </button>
                      </div>
                    )}
                  </div>
                </div>
              </div>
              <p className="text-slate-500 dark:text-slate-400 text-sm">
                {group.description || 'No description provided.'}
              </p>

              <button
                type="button"
                onClick={() => navigate(`/groups/${group.id}`)}
                className="mt-5 w-full rounded-lg bg-slate-900 px-4 py-2.5 text-sm font-medium text-white transition hover:bg-slate-800 dark:bg-slate-700 dark:hover:bg-slate-600"
              >
                Open workspace
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function formatGroupToken(groupId: number) {
  return String(groupId).padStart(9, '0');
}
