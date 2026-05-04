import { useState, useEffect, useCallback } from 'react';

/**
 * Same-origin proxy (pages/api/backend/...) → Render Flask admin.
 * Set ADMIN_BACKEND_URL or NEXT_PUBLIC_ADMIN_BACKEND_URL on Vercel (server reads both for the proxy).
 * Direct browser→Render calls often fail (CORS, cold start, env not baked into client).
 */
const API = '/api/backend';

export default function AdminPanel() {
  const [groups, setGroups] = useState([]);
  const [drivers, setDrivers] = useState([]);
  const [assistantsByGroup, setAssistantsByGroup] = useState({});
  const [settings, setSettings] = useState({
    assistants_choose_group: false,
    st_telegram_id: '',
    receipt_detection_mode: 'lax',
  });
  const [contactSources, setContactSources] = useState([]);
  const [assignments, setAssignments] = useState([]);
  const [stats, setStats] = useState({ total_leads: 0, drivers: [] });
  const [receiptDebtsDrivers, setReceiptDebtsDrivers] = useState([]);
  const [receiptModalOpen, setReceiptModalOpen] = useState(false);
  const [receiptModalDriver, setReceiptModalDriver] = useState(null); // { driver_id, driver_name }
  const [receiptModalItems, setReceiptModalItems] = useState([]); // pending receipt assignments
  const [receiptSelectedAssignmentId, setReceiptSelectedAssignmentId] = useState(null);
  const [receiptModalLoading, setReceiptModalLoading] = useState(false);
  const [submittedReceipts, setSubmittedReceipts] = useState([]);
  const [upcomingRenewals, setUpcomingRenewals] = useState([]);
  const [loading, setLoading] = useState(true);
  const [message, setMessage] = useState(null);
  const [messageType, setMessageType] = useState('success');

  const showMessage = (msg, type = 'success') => {
    setMessage(msg);
    setMessageType(type);
    setTimeout(() => setMessage(null), 5000);
  };

  const fetchGroups = useCallback(async () => {
    try {
      const res = await fetch(`${API}/api/groups`);
      if (res.ok) setGroups(await res.json());
      else setGroups([]);
    } catch {
      setGroups([]);
    }
  }, []);

  const fetchDrivers = useCallback(async () => {
    try {
      const res = await fetch(`${API}/api/drivers`);
      if (res.ok) setDrivers(await res.json());
      else setDrivers([]);
    } catch {
      setDrivers([]);
    }
  }, []);

  const fetchAssistantsForGroups = useCallback(async (groupList) => {
    const next = {};
    for (const g of groupList || []) {
      try {
        const res = await fetch(`${API}/api/groups/${g.id}/assistants`);
        next[g.id] = res.ok ? await res.json() : [];
      } catch {
        next[g.id] = [];
      }
    }
    setAssistantsByGroup(next);
  }, []);

  const fetchSettings = useCallback(async () => {
    try {
      const res = await fetch(`${API}/api/settings`);
      if (res.ok) {
        const data = await res.json();
        setSettings({
          assistants_choose_group: !!data.assistants_choose_group,
          st_telegram_id: data.st_telegram_id || '',
          receipt_detection_mode: data.receipt_detection_mode === 'strict' ? 'strict' : 'lax',
        });
      }
    } catch {}
  }, []);

  const fetchStats = useCallback(async () => {
    try {
      const res = await fetch(`${API}/api/stats`);
      if (res.ok) setStats(await res.json());
    } catch {}
  }, []);

  const fetchSubmittedReceipts = useCallback(async () => {
    try {
      const res = await fetch(`${API}/api/receipts/submitted?limit=100`);
      if (res.ok) {
        const data = await res.json();
        setSubmittedReceipts(Array.isArray(data) ? data : []);
      } else {
        setSubmittedReceipts([]);
      }
    } catch {
      setSubmittedReceipts([]);
    }
  }, []);

  const fetchReceiptDebts = useCallback(async () => {
    try {
      const res = await fetch(`${API}/api/receipt_debts/summary`);
      if (res.ok) {
        const data = await res.json();
        setReceiptDebtsDrivers(data?.drivers || []);
      } else {
        const status = res.status;
        showMessage(
          `Receipt tracker API not available (HTTP ${status}). Redeploy the Render admin backend to include the new /api/receipt_debts routes.`,
          'error'
        );
        setReceiptDebtsDrivers([]);
      }
    } catch {
      showMessage(
        'Could not reach the admin backend. In Vercel set ADMIN_BACKEND_URL (or NEXT_PUBLIC_ADMIN_BACKEND_URL) to your Render URL and redeploy.',
        'error'
      );
      setReceiptDebtsDrivers([]);
    }
  }, []);

  const fetchUpcomingRenewals = useCallback(async () => {
    try {
      const res = await fetch(`${API}/api/renewals/upcoming`);
      if (res.ok) {
        const data = await res.json();
        setUpcomingRenewals(Array.isArray(data) ? data : []);
      } else {
        setUpcomingRenewals([]);
      }
    } catch {
      setUpcomingRenewals([]);
    }
  }, []);

  const fetchContactSources = useCallback(async () => {
    try {
      const res = await fetch(`${API}/api/contact_sources`);
      if (res.ok) setContactSources(await res.json());
      else setContactSources([]);
    } catch {
      setContactSources([]);
    }
  }, []);

  const fetchAssignments = useCallback(async () => {
    try {
      const res = await fetch(`${API}/api/assignments`);
      if (res.ok) setAssignments(await res.json());
      else setAssignments([]);
    } catch {
      setAssignments([]);
    }
  }, []);

  useEffect(() => {
    (async () => {
      setLoading(true);
      await Promise.all([
        fetchGroups(),
        fetchDrivers(),
        fetchSettings(),
        fetchStats(),
        fetchReceiptDebts(),
        fetchSubmittedReceipts(),
        fetchContactSources(),
        fetchAssignments(),
        fetchUpcomingRenewals(),
      ]);
      setLoading(false);
    })();
  }, [fetchGroups, fetchDrivers, fetchSettings, fetchStats, fetchReceiptDebts, fetchSubmittedReceipts, fetchContactSources, fetchAssignments, fetchUpcomingRenewals]);

  useEffect(() => {
    if (groups.length === 0) return;
    fetchAssistantsForGroups(groups);
  }, [API, groups, fetchAssistantsForGroups]);

  async function handleAddGroup(e) {
    e.preventDefault();
    const form = e.target;
    const payload = {
      group_name: form.group_name.value.trim(),
      group_telegram_id: form.group_telegram_id.value.trim(),
      supervisory_telegram_id: form.supervisory_telegram_id.value.trim(),
    };
    if (!payload.group_name || !payload.group_telegram_id || !payload.supervisory_telegram_id) {
      showMessage('Fill all group fields.', 'error');
      return;
    }
    try {
      const res = await fetch(`${API}/api/groups`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json().catch(() => ({}));
      if (res.ok && data.success) {
        showMessage(data.message || 'Group added!');
        form.reset();
        fetchGroups();
      } else {
        showMessage(data.error || 'Failed to add group', 'error');
      }
    } catch (err) {
      showMessage('Network error. Is the backend running?', 'error');
    }
  }

  async function handleAddDriver(e) {
    e.preventDefault();
    const form = e.target;
    const payload = {
      driver_name: form.driver_name.value.trim(),
      driver_telegram_id: form.driver_telegram_id.value.trim(),
      phone_number: (form.phone_number?.value || '').trim() || null,
    };
    if (!payload.driver_name || !payload.driver_telegram_id) {
      showMessage('Fill driver name and Telegram ID.', 'error');
      return;
    }
    try {
      const res = await fetch(`${API}/api/drivers`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json().catch(() => ({}));
      if (res.ok && data.success) {
        showMessage(data.message || 'Driver added!');
        form.reset();
        fetchDrivers();
      } else {
        const msg = data.error || (res.status === 500 ? 'Server error. Check Render logs for details.' : 'Failed to add driver');
        showMessage(msg, 'error');
      }
    } catch (err) {
      showMessage('Network error. Is the backend running?', 'error');
    }
  }

  async function toggleGroup(groupId) {
    try {
      const res = await fetch(`${API}/api/groups/${groupId}/toggle`, { method: 'POST' });
      const data = await res.json().catch(() => ({}));
      if (res.ok && data.success) {
        showMessage(data.message || 'Group updated!');
        fetchGroups();
      } else {
        showMessage(data.error || 'Failed to update group', 'error');
      }
    } catch {
      showMessage('Network error.', 'error');
    }
  }

  async function toggleDriver(driverId) {
    try {
      const res = await fetch(`${API}/api/drivers/${driverId}/toggle`, { method: 'POST' });
      const data = await res.json().catch(() => ({}));
      if (res.ok && data.success) {
        showMessage(data.message || 'Driver updated!');
        fetchDrivers();
      } else {
        showMessage(data.error || 'Failed to update driver', 'error');
      }
    } catch {
      showMessage('Network error.', 'error');
    }
  }

  async function openReceiptDebtModal(driverId, assignmentIdToSelect = null) {
    if (!driverId) return;
    setReceiptModalLoading(true);
    try {
      const summaryDriver = receiptDebtsDrivers.find((d) => d.driver_id === driverId);
      const fallbackDriver = drivers.find((d) => d.id === driverId);
      const driverName = summaryDriver?.driver_name || fallbackDriver?.driver_name || 'N/A';

      const res = await fetch(`${API}/api/receipt_debts/drivers/${driverId}`);
      const items = res.ok ? await res.json() : [];

      setReceiptModalDriver({ driver_id: driverId, driver_name: driverName });
      setReceiptModalItems(items || []);
      const nextSelected = assignmentIdToSelect || (items?.[0]?.assignment_id ?? null);
      setReceiptSelectedAssignmentId(nextSelected);
      setReceiptModalOpen(true);
    } catch {
      showMessage('Failed to load pending receipts.', 'error');
      setReceiptModalOpen(false);
    } finally {
      setReceiptModalLoading(false);
    }
  }

  async function clearDriverPendingReceipts(driverId) {
    if (!driverId) return;
    const ok = window.confirm('Delete ALL pending (unsent) receipts for this driver? This clears the penalty used by the bot.');
    if (!ok) return;

    try {
      const res = await fetch(`${API}/api/receipt_debts/drivers/${driverId}/pending`, { method: 'DELETE' });
      const data = await res.json().catch(() => ({}));
      if (res.ok && data.success) {
        showMessage(`Cleared pending receipts (${data.deleted || 0}).`);
        await fetchReceiptDebts();
        await fetchSubmittedReceipts();
        // Refresh modal items if we are viewing this driver.
        if (receiptModalDriver?.driver_id === driverId) {
          await openReceiptDebtModal(driverId);
        }
      } else {
        showMessage(data.error || 'Failed to clear pending receipts.', 'error');
      }
    } catch {
      showMessage('Network error.', 'error');
    }
  }

  async function deletePendingReceiptAssignment(assignmentId) {
    if (!assignmentId) return;
    const ok = window.confirm('Delete this pending receipt assignment (unsent receipt)?');
    if (!ok) return;

    try {
      const res = await fetch(`${API}/api/receipt_debts/assignments/${assignmentId}`, { method: 'DELETE' });
      const data = await res.json().catch(() => ({}));
      if (res.ok && data.success) {
        showMessage('Pending receipt deleted.');
        await fetchReceiptDebts();
        await fetchSubmittedReceipts();
        if (receiptModalDriver?.driver_id) {
          await openReceiptDebtModal(receiptModalDriver.driver_id);
        }
      } else {
        showMessage(data.error || 'Failed to delete pending receipt.', 'error');
      }
    } catch {
      showMessage('Network error.', 'error');
    }
  }

  async function handleAddAssistant(groupId, telegramId) {
    const tid = String(telegramId).trim();
    if (!tid) {
      showMessage('Enter assistant Telegram ID.', 'error');
      return;
    }
    try {
      const res = await fetch(`${API}/api/groups/${groupId}/assistants`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ telegram_id: tid }),
      });
      const data = await res.json().catch(() => ({}));
      if (res.ok && data.success) {
        showMessage(data.message || 'Assistant added!');
        fetchAssistantsForGroups(groups);
      } else {
        showMessage(data.error || 'Failed to add assistant', 'error');
      }
    } catch {
      showMessage('Network error.', 'error');
    }
  }

  async function handleRemoveAssistant(groupId, telegramId) {
    try {
      const res = await fetch(`${API}/api/groups/${groupId}/assistants/${encodeURIComponent(String(telegramId))}`, { method: 'DELETE' });
      const data = await res.json().catch(() => ({}));
      if (res.ok && data.success) {
        showMessage(data.message || 'Assistant removed!');
        setAssistantsByGroup((prev) => ({ ...prev, [groupId]: (prev[groupId] || []).filter((t) => t !== String(telegramId)) }));
      } else {
        showMessage(data.error || 'Failed to remove assistant', 'error');
      }
    } catch {
      showMessage('Network error.', 'error');
    }
  }

  async function toggleAssistantsChooseGroup() {
    const next = !settings.assistants_choose_group;
    try {
      const res = await fetch(`${API}/api/settings`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ assistants_choose_group: next }),
      });
      const data = await res.json().catch(() => ({}));
      if (res.ok && data.success) {
        setSettings((s) => ({ ...s, assistants_choose_group: next }));
        showMessage(data.message || 'Setting updated!');
      } else {
        showMessage(data.error || 'Failed to update setting', 'error');
      }
    } catch {
      showMessage('Network error.', 'error');
    }
  }

  async function setReceiptDetectionMode(mode) {
    const m = mode === 'strict' ? 'strict' : 'lax';
    try {
      const res = await fetch(`${API}/api/settings`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ receipt_detection_mode: m }),
      });
      const data = await res.json().catch(() => ({}));
      if (res.ok && data.success) {
        setSettings((s) => ({ ...s, receipt_detection_mode: m }));
        showMessage(data.message || 'Receipt detection updated!');
      } else {
        showMessage(data.error || 'Failed to update receipt mode', 'error');
      }
    } catch {
      showMessage('Network error.', 'error');
    }
  }

  async function saveStTelegramId(e) {
    e.preventDefault();
    const val = (e.target.st_telegram_id?.value ?? '').trim();
    try {
      const res = await fetch(`${API}/api/settings`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ st_telegram_id: val }),
      });
      const data = await res.json().catch(() => ({}));
      if (res.ok && data.success) {
        setSettings((s) => ({ ...s, st_telegram_id: val }));
        showMessage(data.message || 'ST Telegram ID saved!');
      } else {
        showMessage(data.error || 'Failed to save', 'error');
      }
    } catch {
      showMessage('Network error.', 'error');
    }
  }

  async function handleAddContactSource(e) {
    e.preventDefault();
    const label = (e.target.label?.value ?? '').trim();
    if (!label) {
      showMessage('Enter a label.', 'error');
      return;
    }
    try {
      const res = await fetch(`${API}/api/contact_sources`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ label, sort_order: contactSources.length }),
      });
      const data = await res.json().catch(() => ({}));
      if (res.ok && data.success) {
        showMessage(data.message || 'Contact source added!');
        e.target.reset();
        fetchContactSources();
      } else {
        showMessage(data.error || 'Failed to add contact source', 'error');
      }
    } catch {
      showMessage('Network error.', 'error');
    }
  }

  async function toggleContactSource(sourceId) {
    try {
      const res = await fetch(`${API}/api/contact_sources/${sourceId}/toggle`, { method: 'POST' });
      const data = await res.json().catch(() => ({}));
      if (res.ok && data.success) {
        showMessage(data.message || 'Contact source updated!');
        fetchContactSources();
      } else {
        showMessage(data.error || 'Failed to update', 'error');
      }
    } catch {
      showMessage('Network error.', 'error');
    }
  }

  async function handleAssignDriver(e) {
    e.preventDefault();
    const groupId = (e.target.group_id?.value ?? '').trim();
    const driverId = (e.target.driver_id?.value ?? '').trim();
    if (!groupId || !driverId) {
      showMessage('Select group and driver.', 'error');
      return;
    }
    try {
      const res = await fetch(`${API}/api/assignments`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ group_id: groupId, driver_id: driverId }),
      });
      const data = await res.json().catch(() => ({}));
      if (res.ok && data.success) {
        showMessage(data.message || 'Driver assigned!');
        fetchAssignments();
      } else {
        showMessage(data.error || 'Failed to assign (maybe already assigned)', 'error');
      }
    } catch {
      showMessage('Network error.', 'error');
    }
  }

  async function removeAssignment(assignmentId) {
    try {
      const res = await fetch(`${API}/api/assignments/${assignmentId}`, { method: 'DELETE' });
      const data = await res.json().catch(() => ({}));
      if (res.ok && data.success) {
        showMessage(data.message || 'Assignment removed!');
        fetchAssignments();
      } else {
        showMessage(data.error || 'Failed to remove', 'error');
      }
    } catch {
      showMessage('Network error.', 'error');
    }
  }

  if (loading) {
    return (
      <div style={styles.page}>
        <div style={styles.container}>
          <p style={{ textAlign: 'center', color: '#666' }}>Loading...</p>
        </div>
      </div>
    );
  }

  const receiptSelectedItem =
    receiptModalItems.find((it) => it.assignment_id === receiptSelectedAssignmentId) || null;

  return (
    <div className="admin-page" style={styles.page}>
      <style dangerouslySetInnerHTML={{ __html: `
        .admin-page { padding: 20px; }
        .admin-container { max-width: 1200px; margin: 0 auto; background: white; border-radius: 12px; box-shadow: 0 20px 60px rgba(0,0,0,0.3); padding: 30px; }
        .admin-table-wrap { overflow-x: auto; -webkit-overflow-scrolling: touch; margin-top: 15px; }
        .admin-table-wrap table { min-width: 320px; }
        .admin-section { margin-bottom: 40px; padding: 20px; background: #f8f9fa; border-radius: 8px; }
        .admin-assistant-form { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
        .admin-assistant-form input { flex: 1; min-width: 0; }
        @media (max-width: 768px) {
          .admin-page { padding: 10px; }
          .admin-container { padding: 16px; }
          .admin-section { padding: 14px; margin-bottom: 24px; }
          .admin-section h2 { font-size: 1rem; margin-bottom: 14px; }
          .admin-table-wrap th, .admin-table-wrap td { padding: 10px 6px; font-size: 0.8125rem; }
          .admin-mobile-full button { width: 100%; min-height: 48px; }
          .admin-assistant-form { flex-direction: column; align-items: stretch; }
          .admin-assistant-form button { width: 100%; }
          .admin-assistant-form input { width: 100%; }
        }
        @media (max-width: 480px) {
          .admin-section h2 { font-size: 0.9375rem; }
        }
      ` }} />
      <div className="admin-container" style={styles.container}>
        <h1 style={styles.h1}>KrabsLeads Admin</h1>

        {message && (
          <div
            style={{
              ...styles.message,
              ...(messageType === 'error' ? styles.messageError : styles.messageSuccess),
            }}
          >
            {message}
          </div>
        )}

        <section className="admin-section" style={styles.section}>
          <h2 style={styles.sectionTitle}>Lead flow</h2>
          <p style={{ marginBottom: 10, color: '#555' }}>
            When <strong>Allow assistants to choose group</strong> is ON, anyone can send leads and will choose a group (then a driver). When OFF, assistants use their assigned group.
          </p>
          <p style={{ marginBottom: 12 }}><strong>Current:</strong> {settings.assistants_choose_group ? 'Allow assistants to choose group' : 'Use assigned groups only'}</p>
          <button type="button" onClick={toggleAssistantsChooseGroup} className="admin-mobile-full" style={styles.button}>
            {settings.assistants_choose_group ? 'Use assigned groups only' : 'Allow assistants to choose group'}
          </button>
          <div style={{ marginTop: 20, paddingTop: 16, borderTop: '1px solid #dee2e6' }}>
            <p style={{ marginBottom: 8, fontWeight: 700 }}>Driver receipt detection (AI)</p>
            <p style={{ marginBottom: 12, color: '#555', fontSize: '0.95rem' }}>
              <strong>Strict</strong>: image must look like a receipt and show a visible <strong>$</strong> (no check that the amount matches the lead).
              {' '}
              <strong>Lax</strong>: same receipt check, and the paid total must match the lead price when a price is set.
            </p>
            <p style={{ marginBottom: 12 }}>
              <strong>Current:</strong>{' '}
              {settings.receipt_detection_mode === 'strict' ? 'Strict ($ on receipt)' : 'Lax (match amount to lead)'}
            </p>
            <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
              <button
                type="button"
                onClick={() => setReceiptDetectionMode('strict')}
                className="admin-mobile-full"
                style={{
                  ...styles.button,
                  opacity: settings.receipt_detection_mode === 'strict' ? 1 : 0.75,
                  fontWeight: settings.receipt_detection_mode === 'strict' ? 700 : 400,
                }}
              >
                Use strict ($ only)
              </button>
              <button
                type="button"
                onClick={() => setReceiptDetectionMode('lax')}
                className="admin-mobile-full"
                style={{
                  ...styles.button,
                  opacity: settings.receipt_detection_mode === 'lax' ? 1 : 0.75,
                  fontWeight: settings.receipt_detection_mode === 'lax' ? 700 : 400,
                }}
              >
                Use lax (match amount)
              </button>
            </div>
          </div>
        </section>

        <section className="admin-section" style={styles.section}>
          <h2 style={styles.sectionTitle}>ST Telegram ID</h2>
          <p style={{ marginBottom: 12, color: '#555' }}>Notified when a lead is successfully sent and when a driver submits a receipt.</p>
          <form onSubmit={saveStTelegramId} style={styles.form}>
            <div style={styles.formGroup}>
              <label>ST Telegram ID</label>
              <input type="text" name="st_telegram_id" defaultValue={settings.st_telegram_id} placeholder="e.g. 123456789" style={styles.input} />
            </div>
            <button type="submit" className="admin-mobile-full" style={styles.button}>Save</button>
          </form>
        </section>

        <section className="admin-section" style={styles.section}>
          <h2 style={styles.sectionTitle}>Lead stats</h2>
          <p style={{ marginBottom: 12 }}><strong>Total leads sent:</strong> {stats.total_leads ?? 0}</p>
          <div className="admin-table-wrap">
          <table style={styles.table}>
            <thead>
              <tr>
                <th>Driver</th>
                <th>Leads accepted</th>
                <th>Receipts submitted</th>
              </tr>
            </thead>
            <tbody>
              {(stats.drivers || []).length === 0 ? (
                <tr><td colSpan={3} style={{ textAlign: 'center', color: '#888' }}>No drivers</td></tr>
              ) : (
                (stats.drivers || []).map((d) => (
                  <tr key={d.driver_id}>
                    <td>{d.driver_name}</td>
                    <td>{d.leads_accepted}</td>
                    <td>{d.receipts_submitted}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
          </div>
        </section>

        <section className="admin-section" style={styles.section}>
          <h2 style={styles.sectionTitle}>Receipt Tracker (Owed)</h2>
          <p style={{ marginBottom: 12, color: '#555' }}>
            Owed receipts are accepted leads with an empty/missing receipt image. Deleting pending items clears the penalty used by the bot.
          </p>
          <div className="admin-table-wrap">
            <table style={styles.table}>
              <thead>
                <tr>
                  <th>Driver</th>
                  <th>Owed receipts</th>
                  <th>Pending refs</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {(receiptDebtsDrivers || []).length === 0 ? (
                  <tr><td colSpan={4} style={{ textAlign: 'center', color: '#888' }}>No pending receipts found</td></tr>
                ) : (
                  (receiptDebtsDrivers || []).map((d) => (
                    <tr key={d.driver_id}>
                      <td>{d.driver_name}</td>
                      <td>{d.owed_receipts || 0}</td>
                      <td>
                        {(d.pending_references || []).length === 0 ? (
                          <span style={{ color: '#888' }}>—</span>
                        ) : (
                          (d.pending_references || []).map((r) => (
                            <button
                              key={r.assignment_id}
                              type="button"
                              className="admin-mobile-full"
                              style={{ ...styles.buttonSmall, marginRight: 8, marginBottom: 8, minWidth: 0, background: '#667eea', color: 'white' }}
                              onClick={() => openReceiptDebtModal(d.driver_id, r.assignment_id)}
                            >
                              {r.reference_id}
                            </button>
                          ))
                        )}
                      </td>
                      <td>
                        <button
                          type="button"
                          onClick={() => openReceiptDebtModal(d.driver_id)}
                          className="admin-mobile-full"
                          style={{ ...styles.button, ...styles.buttonSmall }}
                        >
                          View details
                        </button>
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </section>

        <section className="admin-section" style={styles.section}>
          <h2 style={styles.sectionTitle}>Submitted receipts</h2>
          <p style={{ marginBottom: 12, color: '#555' }}>
            Driver-uploaded receipt images stored on each lead. Open full size in a new tab if the preview does not load (some Telegram file URLs expire).
          </p>
          <div style={{ marginBottom: 12 }}>
            <button type="button" className="admin-mobile-full" style={{ ...styles.button, ...styles.buttonSmall }} onClick={() => fetchSubmittedReceipts()}>
              Refresh list
            </button>
          </div>
          <div className="admin-table-wrap">
            <table style={styles.table}>
              <thead>
                <tr>
                  <th>Ref</th>
                  <th>Driver</th>
                  <th>Group</th>
                  <th>Receipt</th>
                  <th>Updated</th>
                </tr>
              </thead>
              <tbody>
                {(submittedReceipts || []).length === 0 ? (
                  <tr><td colSpan={5} style={{ textAlign: 'center', color: '#888' }}>No submitted receipts yet</td></tr>
                ) : (
                  (submittedReceipts || []).map((row) => (
                    <tr key={row.lead_id}>
                      <td><code>{row.reference_id}</code></td>
                      <td>{row.driver_name}</td>
                      <td>{row.group_name}</td>
                      <td>
                        {row.receipt_image_url ? (
                          <div style={{ display: 'flex', flexDirection: 'column', gap: 8, alignItems: 'flex-start' }}>
                            <a href={row.receipt_image_url} target="_blank" rel="noopener noreferrer" style={{ fontSize: 13 }}>
                              Open full image
                            </a>
                            <img
                              src={row.receipt_image_url}
                              alt={`Receipt ${row.reference_id}`}
                              style={{ maxWidth: 180, maxHeight: 240, objectFit: 'contain', borderRadius: 6, border: '1px solid #ddd' }}
                            />
                          </div>
                        ) : (
                          '—'
                        )}
                      </td>
                      <td style={{ fontSize: 12, color: '#555' }}>{row.updated_at ? String(row.updated_at).slice(0, 19) : '—'}</td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </section>

        <section className="admin-section" style={styles.section}>
          <h2 style={styles.sectionTitle}>Contact info sources</h2>
          <p style={{ marginBottom: 12, color: '#555' }}>Options shown after a lead is sent (e.g. &quot;Select the Contact info source for this client&quot;).</p>
          <form onSubmit={handleAddContactSource} style={{ ...styles.form, marginBottom: 16 }}>
            <div style={styles.formGroup}>
              <label>New source label</label>
              <input type="text" name="label" placeholder="e.g. Blue FB" style={styles.input} required />
            </div>
            <button type="submit" className="admin-mobile-full" style={styles.button}>Add contact source</button>
          </form>
          <div className="admin-table-wrap">
            <table style={styles.table}>
              <thead>
                <tr>
                  <th>Label</th>
                  <th>Status</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {(contactSources || []).length === 0 ? (
                  <tr><td colSpan={3} style={{ textAlign: 'center', color: '#888' }}>No contact sources yet</td></tr>
                ) : (
                  (contactSources || []).map((s) => (
                    <tr key={s.id}>
                      <td>{s.label}</td>
                      <td>
                        <span style={s.is_active !== false ? styles.statusActive : styles.statusInactive}>
                          {s.is_active !== false ? 'Active' : 'Inactive'}
                        </span>
                      </td>
                      <td>
                        <button type="button" onClick={() => toggleContactSource(s.id)} className="admin-mobile-full" style={{ ...styles.button, ...styles.buttonSmall, ...styles.buttonDanger }}>
                          {s.is_active !== false ? 'Deactivate' : 'Activate'}
                        </button>
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </section>

        <section className="admin-section" style={styles.section}>
          <h2 style={styles.sectionTitle}>Add New Group</h2>
          <form onSubmit={handleAddGroup} style={styles.form}>
            <div style={styles.formGroup}>
              <label>Group Name</label>
              <input type="text" name="group_name" required placeholder="e.g. Group A" style={styles.input} />
            </div>
            <div style={styles.formGroup}>
              <label>Group Telegram ID</label>
              <input type="text" name="group_telegram_id" required placeholder="e.g. -1001234567890" style={styles.input} />
            </div>
            <div style={styles.formGroup}>
              <label>Supervisory Telegram ID</label>
              <input type="text" name="supervisory_telegram_id" required placeholder="e.g. 123456789" style={styles.input} />
            </div>
            <button type="submit" className="admin-mobile-full" style={styles.button}>Add Group</button>
          </form>
        </section>

        <section className="admin-section" style={styles.section}>
          <h2 style={styles.sectionTitle}>Add New Driver</h2>
          <form onSubmit={handleAddDriver} style={styles.form}>
            <div style={styles.formGroup}>
              <label>Driver Name</label>
              <input type="text" name="driver_name" required placeholder="e.g. John Doe" style={styles.input} />
            </div>
            <div style={styles.formGroup}>
              <label>Driver Telegram ID</label>
              <input type="text" name="driver_telegram_id" required placeholder="e.g. 123456789" style={styles.input} />
            </div>
            <div style={styles.formGroup}>
              <label>Phone (optional)</label>
              <input type="text" name="phone_number" placeholder="e.g. +1234567890" style={styles.input} />
            </div>
            <button type="submit" className="admin-mobile-full" style={styles.button}>Add Driver</button>
          </form>
        </section>

        <section className="admin-section" style={styles.section}>
          <h2 style={styles.sectionTitle}>Assign driver to group</h2>
          <p style={{ marginBottom: 12, color: '#555' }}>Drivers in a group can receive leads sent to that group.</p>
          <form onSubmit={handleAssignDriver} style={{ ...styles.form, marginBottom: 16 }}>
            <div style={styles.formGroup}>
              <label>Group</label>
              <select name="group_id" style={styles.input} required>
                <option value="">— Select group —</option>
                {(groups || []).map((g) => (
                  <option key={g.id} value={g.id}>{g.group_name}</option>
                ))}
              </select>
            </div>
            <div style={styles.formGroup}>
              <label>Driver</label>
              <select name="driver_id" style={styles.input} required>
                <option value="">— Select driver —</option>
                {(drivers || []).map((d) => (
                  <option key={d.id} value={d.id}>{d.driver_name}</option>
                ))}
              </select>
            </div>
            <button type="submit" className="admin-mobile-full" style={styles.button}>Assign driver to group</button>
          </form>
          <div className="admin-table-wrap">
            <table style={styles.table}>
              <thead>
                <tr>
                  <th>Group</th>
                  <th>Driver</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {(assignments || []).length === 0 ? (
                  <tr><td colSpan={3} style={{ textAlign: 'center', color: '#888' }}>No assignments yet</td></tr>
                ) : (
                  (assignments || []).map((a) => (
                    <tr key={a.id}>
                      <td>{a.group_name}</td>
                      <td>{a.driver_name}</td>
                      <td>
                        <button type="button" onClick={() => removeAssignment(a.id)} className="admin-mobile-full" style={{ ...styles.button, ...styles.buttonSmall, ...styles.buttonDanger }}>
                          Remove
                        </button>
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </section>

        <section className="admin-section" style={styles.section}>
          <h2 style={styles.sectionTitle}>Groups</h2>
          <div className="admin-table-wrap">
          <table style={styles.table}>
            <thead>
              <tr>
                <th>Name</th>
                <th>Group ID</th>
                <th>Supervisory ID</th>
                <th>Status</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {groups.length === 0 ? (
                <tr><td colSpan={5} style={{ textAlign: 'center', color: '#888' }}>No groups yet</td></tr>
              ) : (
                groups.map((g) => (
                  <tr key={g.id}>
                    <td>{g.group_name}</td>
                    <td><code>{g.group_telegram_id}</code></td>
                    <td><code>{g.supervisory_telegram_id}</code></td>
                    <td>
                      <span style={g.is_active ? styles.statusActive : styles.statusInactive}>
                        {g.is_active ? 'Active' : 'Inactive'}
                      </span>
                    </td>
                    <td>
                      <button
                        type="button"
                        onClick={() => toggleGroup(g.id)}
                        className="admin-mobile-full"
                        style={{ ...styles.button, ...styles.buttonSmall, ...styles.buttonDanger }}
                      >
                        {g.is_active ? 'Deactivate' : 'Activate'}
                      </button>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
          </div>
        </section>

        <section className="admin-section" style={styles.section}>
          <h2 style={styles.sectionTitle}>Group Assistants</h2>
          <p style={{ marginBottom: 16, color: '#555' }}>Assistants use the bot like normal; their leads go to the group they are assigned to.</p>
          {groups.map((g) => (
            <div key={g.id} style={{ marginBottom: 20, padding: 12, background: '#fff', borderRadius: 6, border: '1px solid #ddd' }}>
              <strong>{g.group_name}</strong> — Assistants (Telegram IDs):
              <ul style={{ margin: '8px 0', paddingLeft: 20 }}>
                {(assistantsByGroup[g.id] || []).length === 0 ? (
                  <li style={{ color: '#888' }}>None yet</li>
                ) : (
                  (assistantsByGroup[g.id] || []).map((tid) => (
                    <li key={tid} style={{ marginBottom: 6 }}>
                      <code>{tid}</code>{' '}
                      <button
                        type="button"
                        onClick={() => handleRemoveAssistant(g.id, tid)}
                        className="admin-mobile-full"
                        style={{ ...styles.button, ...styles.buttonSmall, ...styles.buttonDanger, marginLeft: 8, marginTop: 4 }}
                      >
                        Remove
                      </button>
                    </li>
                  ))
                )}
              </ul>
              <form
                className="admin-assistant-form"
                onSubmit={(e) => {
                  e.preventDefault();
                  const input = e.target.querySelector('input[name="telegram_id"]');
                  if (input) {
                    handleAddAssistant(g.id, input.value);
                    input.value = '';
                  }
                }}
              >
                <input
                  type="text"
                  name="telegram_id"
                  placeholder="Assistant Telegram ID (e.g. 123456789)"
                  style={styles.input}
                />
                <button type="submit" className="admin-mobile-full" style={styles.button}>Add Assistant</button>
              </form>
            </div>
          ))}
        </section>

        <section className="admin-section" style={styles.section}>
          <h2 style={styles.sectionTitle}>Drivers</h2>
          <div className="admin-table-wrap">
          <table style={styles.table}>
            <thead>
              <tr>
                <th>Name</th>
                <th>Telegram ID</th>
                <th>Phone</th>
                <th>Status</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {drivers.length === 0 ? (
                <tr><td colSpan={5} style={{ textAlign: 'center', color: '#888' }}>No drivers yet</td></tr>
              ) : (
                drivers.map((d) => (
                  <tr key={d.id}>
                    <td>{d.driver_name}</td>
                    <td><code>{d.driver_telegram_id}</code></td>
                    <td>{d.phone_number || '—'}</td>
                    <td>
                      <span style={d.is_active ? styles.statusActive : styles.statusInactive}>
                        {d.is_active ? 'Active' : 'Inactive'}
                      </span>
                    </td>
                    <td>
                      <button
                        type="button"
                        onClick={() => toggleDriver(d.id)}
                        className="admin-mobile-full"
                        style={{ ...styles.button, ...styles.buttonSmall, ...styles.buttonDanger }}
                      >
                        {d.is_active ? 'Deactivate' : 'Activate'}
                      </button>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
          </div>
        </section>

        {receiptModalOpen && (
          <div
            style={{
              position: 'fixed',
              top: 0,
              left: 0,
              width: '100%',
              height: '100%',
              background: 'rgba(0,0,0,0.6)',
              zIndex: 9999,
              padding: 20,
              overflow: 'auto',
            }}
            onClick={(e) => {
              if (e.target === e.currentTarget) setReceiptModalOpen(false);
            }}
          >
            <div
              style={{
                background: 'white',
                borderRadius: 12,
                boxShadow: '0 20px 60px rgba(0,0,0,0.3)',
                maxWidth: 1100,
                margin: '0 auto',
                padding: 20,
              }}
            >
              <div style={{ display: 'flex', gap: 12, alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap' }}>
                <div>
                  <h2 style={{ margin: 0, color: '#333' }}>
                    Pending receipts: {receiptModalDriver?.driver_name || '—'}
                  </h2>
                  <p style={{ marginTop: 6, marginBottom: 0, color: '#555' }}>
                    {receiptModalItems.length} pending unsent receipt(s)
                  </p>
                </div>
                <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                  <button
                    type="button"
                    onClick={() => clearDriverPendingReceipts(receiptModalDriver?.driver_id)}
                    className="admin-mobile-full"
                    style={{ ...styles.button, ...styles.buttonSmall, background: '#dc3545' }}
                    disabled={receiptModalLoading || !receiptModalDriver?.driver_id || receiptModalItems.length === 0}
                  >
                    Clear all
                  </button>
                  <button
                    type="button"
                    onClick={() => setReceiptModalOpen(false)}
                    className="admin-mobile-full"
                    style={{ ...styles.button, ...styles.buttonSmall }}
                  >
                    Close
                  </button>
                </div>
              </div>

              <div style={{ marginTop: 16 }}>
                {receiptModalLoading ? (
                  <p style={{ color: '#555' }}>Loading pending receipts...</p>
                ) : receiptModalItems.length === 0 ? (
                  <p style={{ color: '#888' }}>No pending receipts for this driver.</p>
                ) : (
                  <div style={{ display: 'flex', gap: 16, alignItems: 'flex-start', flexWrap: 'wrap' }}>
                    <div style={{ flex: '1 1 320px', minWidth: 280 }}>
                      <h3 style={{ marginBottom: 10, color: '#667eea' }}>References</h3>
                      <div style={{ maxHeight: 420, overflow: 'auto', paddingRight: 8 }}>
                        {(receiptModalItems || []).map((it) => {
                          const selected = it.assignment_id === receiptSelectedAssignmentId;
                          return (
                            <div
                              key={it.assignment_id}
                              style={{
                                display: 'flex',
                                gap: 8,
                                alignItems: 'center',
                                padding: 8,
                                borderRadius: 8,
                                border: selected ? '2px solid #667eea' : '1px solid #eee',
                                marginBottom: 8,
                              }}
                            >
                              <button
                                type="button"
                                onClick={() => setReceiptSelectedAssignmentId(it.assignment_id)}
                                style={{
                                  ...styles.buttonSmall,
                                  background: selected ? '#667eea' : '#eee',
                                  color: selected ? 'white' : '#333',
                                  borderRadius: 6,
                                  border: 'none',
                                  cursor: 'pointer',
                                  padding: '8px 10px',
                                  whiteSpace: 'nowrap',
                                }}
                              >
                                {it.reference_id}
                              </button>
                              <button
                                type="button"
                                onClick={() => deletePendingReceiptAssignment(it.assignment_id)}
                                style={{
                                  ...styles.buttonSmall,
                                  background: '#dc3545',
                                  color: 'white',
                                  borderRadius: 6,
                                  border: 'none',
                                  cursor: 'pointer',
                                  padding: '8px 10px',
                                  whiteSpace: 'nowrap',
                                }}
                              >
                                Delete
                              </button>
                            </div>
                          );
                        })}
                      </div>
                    </div>

                    <div style={{ flex: '2 1 520px', minWidth: 320 }}>
                      <h3 style={{ marginBottom: 10, color: '#667eea' }}>Receipt details</h3>
                      {receiptSelectedItem ? (
                        <div>
                          <p style={{ marginBottom: 8 }}>
                            <strong>Reference:</strong> {receiptSelectedItem.reference_id}
                            <br />
                            <strong>Accepted at:</strong> {receiptSelectedItem.accepted_at || '—'}
                            <br />
                            <strong>Lead ID:</strong> {receiptSelectedItem.lead_id || '—'}
                          </p>
                          <p style={{ marginBottom: 8, color: '#555' }}>
                            <strong>Monday status:</strong> {receiptSelectedItem.monday_status || '—'}
                          </p>

                          <div style={{ marginBottom: 12 }}>
                            <strong>Vehicle details:</strong>
                            <pre
                              style={{
                                background: '#f6f6f6',
                                padding: 12,
                                borderRadius: 8,
                                whiteSpace: 'pre-wrap',
                                wordBreak: 'break-word',
                                maxHeight: 220,
                                overflow: 'auto',
                              }}
                            >
                              {receiptSelectedItem.vehicle_details || ''}
                            </pre>
                          </div>

                          <div style={{ marginBottom: 12 }}>
                            <strong>Delivery details:</strong>
                            <pre
                              style={{
                                background: '#f6f6f6',
                                padding: 12,
                                borderRadius: 8,
                                whiteSpace: 'pre-wrap',
                                wordBreak: 'break-word',
                                maxHeight: 220,
                                overflow: 'auto',
                              }}
                            >
                              {receiptSelectedItem.delivery_details || ''}
                            </pre>
                          </div>

                          <div style={{ marginBottom: 12 }}>
                            <strong>Extra info:</strong>
                            <pre
                              style={{
                                background: '#f6f6f6',
                                padding: 12,
                                borderRadius: 8,
                                whiteSpace: 'pre-wrap',
                                wordBreak: 'break-word',
                                maxHeight: 220,
                                overflow: 'auto',
                              }}
                            >
                              {receiptSelectedItem.extra_info || ''}
                            </pre>
                          </div>

                          {(receiptSelectedItem.special_request_issuers ||
                            receiptSelectedItem.special_request_note) ? (
                            <div style={{ marginBottom: 12 }}>
                              <strong>Special request (issuers / group):</strong>
                              <pre
                                style={{
                                  background: '#f6f6f6',
                                  padding: 12,
                                  borderRadius: 8,
                                  whiteSpace: 'pre-wrap',
                                  wordBreak: 'break-word',
                                  maxHeight: 220,
                                  overflow: 'auto',
                                }}
                              >
                                {receiptSelectedItem.special_request_issuers ||
                                  receiptSelectedItem.special_request_note ||
                                  ''}
                              </pre>
                            </div>
                          ) : null}
                          {receiptSelectedItem.special_request_drivers ? (
                            <div style={{ marginBottom: 12 }}>
                              <strong>Special request (drivers only):</strong>
                              <pre
                                style={{
                                  background: '#f6f6f6',
                                  padding: 12,
                                  borderRadius: 8,
                                  whiteSpace: 'pre-wrap',
                                  wordBreak: 'break-word',
                                  maxHeight: 220,
                                  overflow: 'auto',
                                }}
                              >
                                {receiptSelectedItem.special_request_drivers}
                              </pre>
                            </div>
                          ) : null}

                          <button
                            type="button"
                            onClick={() => deletePendingReceiptAssignment(receiptSelectedItem.assignment_id)}
                            className="admin-mobile-full"
                            style={{ ...styles.button, ...styles.buttonSmall, background: '#dc3545' }}
                          >
                            Delete this unsent receipt assignment
                          </button>
                        </div>
                      ) : (
                        <p style={{ color: '#888' }}>Select a reference to view details.</p>
                      )}
                    </div>
                  </div>
                )}
              </div>
            </div>
          </div>
        )}
        {/* ── Upcoming Renewals ── */}
        <section className="admin-section" style={styles.section}>
          <h2 style={styles.sectionTitle}>Upcoming Renewals</h2>
          <p style={{ marginBottom: 12, color: '#555' }}>
            Leads approaching their 28-day renewal window. Sorted by days remaining.
          </p>
          <div className="admin-table-wrap">
            <table style={styles.table}>
              <thead>
                <tr>
                  <th>Ref ID</th>
                  <th>Client</th>
                  <th>Vehicle</th>
                  <th>Issuer Group</th>
                  <th>Driver</th>
                  <th>Days Left</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {upcomingRenewals.length === 0 ? (
                  <tr>
                    <td colSpan={7} style={{ textAlign: 'center', color: '#888' }}>
                      No upcoming renewals
                    </td>
                  </tr>
                ) : (
                  upcomingRenewals.map((r) => (
                    <tr key={r.id}>
                      <td style={{ fontFamily: 'monospace', fontWeight: 600 }}>{r.reference_id}</td>
                      <td>{r.client_name}</td>
                      <td style={{ maxWidth: 180, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {r.vehicle || '—'}
                      </td>
                      <td>{r.group_name}</td>
                      <td>{r.driver_name}</td>
                      <td>
                        <span style={{
                          fontWeight: 700,
                          color: r.days_left === 0 ? '#dc3545'
                            : r.days_left != null && r.days_left <= 3 ? '#fd7e14'
                            : '#28a745',
                        }}>
                          {r.days_left != null ? `${r.days_left} day${r.days_left !== 1 ? 's' : ''}` : '—'}
                        </span>
                      </td>
                      <td>
                        <span style={{
                          padding: '2px 8px',
                          borderRadius: 4,
                          fontSize: 12,
                          fontWeight: 600,
                          background: r.status === 'pending' ? '#fff3cd'
                            : r.status === 'group_phase' ? '#cce5ff'
                            : r.status === 'driver_phase' ? '#d4edda'
                            : '#e2e3e5',
                          color: r.status === 'pending' ? '#856404'
                            : r.status === 'group_phase' ? '#004085'
                            : r.status === 'driver_phase' ? '#155724'
                            : '#383d41',
                        }}>
                          {r.status === 'pending' ? 'Waiting'
                            : r.status === 'group_phase' ? 'Group deciding'
                            : r.status === 'driver_phase' ? 'Driver deciding'
                            : r.status}
                        </span>
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </section>

      </div>
    </div>
  );
}

const styles = {
  page: {
    minHeight: '100vh',
    margin: 0,
    width: '100%',
    fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif",
    background: 'linear-gradient(135deg, #667eea 0%, #764ba2 100%)',
    padding: 20,
    boxSizing: 'border-box',
  },
  container: {
    maxWidth: 1200,
    width: '100%',
    margin: '0 auto',
    background: 'white',
    borderRadius: 12,
    boxShadow: '0 20px 60px rgba(0,0,0,0.3)',
    padding: 30,
    boxSizing: 'border-box',
  },
  h1: {
    color: '#333',
    marginBottom: 30,
    textAlign: 'center',
    fontSize: 'clamp(1.25rem, 4vw, 1.75rem)',
  },
  message: {
    padding: 12,
    borderRadius: 6,
    marginBottom: 20,
  },
  messageSuccess: {
    background: '#d4edda',
    color: '#155724',
    border: '1px solid #c3e6cb',
  },
  messageError: {
    background: '#f8d7da',
    color: '#721c24',
    border: '1px solid #f5c6cb',
  },
  section: {
    marginBottom: 40,
    padding: 20,
    background: '#f8f9fa',
    borderRadius: 8,
  },
  sectionTitle: {
    color: '#667eea',
    marginBottom: 20,
    borderBottom: '2px solid #667eea',
    paddingBottom: 10,
  },
  form: { marginTop: 8 },
  formGroup: { marginBottom: 15 },
  input: {
    width: '100%',
    padding: 12,
    border: '2px solid #ddd',
    borderRadius: 6,
    fontSize: 16,
    boxSizing: 'border-box',
  },
  button: {
    background: '#667eea',
    color: 'white',
    padding: '12px 24px',
    border: 'none',
    borderRadius: 6,
    cursor: 'pointer',
    fontSize: 14,
    fontWeight: 500,
    minHeight: 44,
  },
  buttonSmall: { padding: '6px 12px', fontSize: 12 },
  buttonDanger: { background: '#dc3545' },
  table: {
    width: '100%',
    borderCollapse: 'collapse',
    marginTop: 15,
  },
  statusActive: { color: '#28a745', fontWeight: 600 },
  statusInactive: { color: '#dc3545', fontWeight: 600 },
};
