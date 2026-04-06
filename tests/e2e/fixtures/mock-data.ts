/**
 * Mock data fixtures for E2E tests
 * Used to isolate tests from real backend APIs
 */

export const mockUser = {
  id: 'user-e2e-001',
  email: 'e2e-test@example.com',
  name: 'E2E Test User',
  roles: ['admin', 'viewer'],
  createdAt: new Date().toISOString(),
};

export const mockTenants = [
  {
    id: 'tenant-001',
    name: 'Production',
    environment: 'prod',
    domain: 'prod.example.com',
    status: 'active',
    alertCount: 42,
  },
  {
    id: 'tenant-002',
    name: 'Staging',
    environment: 'staging',
    domain: 'staging.example.com',
    status: 'active',
    alertCount: 15,
  },
  {
    id: 'tenant-003',
    name: 'Development',
    environment: 'dev',
    domain: 'dev.example.com',
    status: 'active',
    alertCount: 8,
  },
];

export const mockGroups = [
  {
    id: 'group-001',
    name: 'Database Team',
    description: 'Handles database alerts and monitoring',
    tenantId: 'tenant-001',
    members: [
      {
        id: 'user-001',
        email: 'alice@example.com',
        name: 'Alice Johnson',
        role: 'admin',
      },
      {
        id: 'user-002',
        email: 'bob@example.com',
        name: 'Bob Smith',
        role: 'member',
      },
    ],
    createdAt: new Date().toISOString(),
  },
  {
    id: 'group-002',
    name: 'Infrastructure Team',
    description: 'Infrastructure and platform alerts',
    tenantId: 'tenant-001',
    members: [
      {
        id: 'user-003',
        email: 'charlie@example.com',
        name: 'Charlie Brown',
        role: 'admin',
      },
    ],
    createdAt: new Date().toISOString(),
  },
  {
    id: 'group-003',
    name: 'Application Team',
    description: 'Application-level alerts',
    tenantId: 'tenant-002',
    members: [
      {
        id: 'user-004',
        email: 'diana@example.com',
        name: 'Diana Prince',
        role: 'admin',
      },
      {
        id: 'user-005',
        email: 'eve@example.com',
        name: 'Eve Wilson',
        role: 'member',
      },
      {
        id: 'user-006',
        email: 'frank@example.com',
        name: 'Frank Miller',
        role: 'member',
      },
    ],
    createdAt: new Date().toISOString(),
  },
];

export const mockAlerts = [
  {
    id: 'alert-001',
    name: 'High CPU Usage',
    severity: 'critical',
    tenantId: 'tenant-001',
    groupId: 'group-001',
    status: 'active',
    threshold: 80,
    currentValue: 92,
    triggeredAt: new Date().toISOString(),
  },
  {
    id: 'alert-002',
    name: 'Database Connection Pool Exhausted',
    severity: 'high',
    tenantId: 'tenant-001',
    groupId: 'group-001',
    status: 'active',
    threshold: 95,
    currentValue: 98,
    triggeredAt: new Date().toISOString(),
  },
  {
    id: 'alert-003',
    name: 'API Response Time Degradation',
    severity: 'warning',
    tenantId: 'tenant-002',
    groupId: 'group-003',
    status: 'active',
    threshold: 500,
    currentValue: 650,
    triggeredAt: new Date().toISOString(),
  },
];

export const mockBatchOperationResponse = {
  success: true,
  operationId: 'batch-op-' + Date.now(),
  operation: 'silent_mode',
  affectedGroups: 3,
  affectedAlerts: 15,
  duration: '245ms',
  message: 'Silent mode enabled for 3 groups (15 alerts)',
};

export const mockOperationHistory = [
  {
    id: 'op-001',
    operation: 'silent_mode',
    status: 'completed',
    groupsAffected: 2,
    alertsAffected: 8,
    createdAt: new Date(Date.now() - 3600000).toISOString(),
  },
  {
    id: 'op-002',
    operation: 'maintenance_mode',
    status: 'completed',
    groupsAffected: 1,
    alertsAffected: 5,
    createdAt: new Date(Date.now() - 7200000).toISOString(),
  },
  {
    id: 'op-003',
    operation: 'silence_update',
    status: 'failed',
    error: 'Permission denied',
    createdAt: new Date(Date.now() - 10800000).toISOString(),
  },
];

export const mockRoutingProfiles = [
  {
    id: 'profile-001',
    name: 'Default Route',
    groupWait: '10s',
    groupInterval: '10s',
    repeatInterval: '4h',
    matchers: [
      {
        name: 'severity',
        value: 'warning',
      },
    ],
  },
  {
    id: 'profile-002',
    name: 'Critical Path',
    groupWait: '1s',
    groupInterval: '5s',
    repeatInterval: '1h',
    matchers: [
      {
        name: 'severity',
        value: 'critical',
      },
    ],
  },
];

export const mockLanguages = [
  { code: 'en', name: 'English' },
  { code: 'zh', name: '中文' },
];

export function getMockDataByTenantId(tenantId: string) {
  return {
    tenant: mockTenants.find((t) => t.id === tenantId),
    groups: mockGroups.filter((g) => g.tenantId === tenantId),
    alerts: mockAlerts.filter((a) => a.tenantId === tenantId),
  };
}

export function getMockGroupById(groupId: string) {
  return mockGroups.find((g) => g.id === groupId);
}

export function getMockUserById(userId: string) {
  const allUsers = mockGroups.flatMap((g) => g.members);
  return allUsers.find((u) => u.id === userId);
}
