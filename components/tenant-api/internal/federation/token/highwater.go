package token

// The AccountID high-water mark (#2928) lives on the token-store ConfigMap.
//
// Why here: the mark has to survive an older copy of `tenant-api-state` being
// put back, so it cannot live in that ConfigMap. This object already exists
// whenever AccountIDs are allocated (both need federation), tenant-api's Role
// already grants get+update on exactly it, and `helm.sh/resource-policy: keep`
// carries it through `helm uninstall`.
//
// Why an annotation and not a data key: federation-gateway mounts this
// ConfigMap whole as a volume, so a data key would become a file inside an
// internet-facing pod and refresh that volume on every allocation. Annotations
// are not projected. mutate() writes the object back with its metadata intact,
// so token writes keep the annotation, and its resourceVersion check makes a
// mark write and a token write retry against each other rather than lose one.

import (
	"context"
	"fmt"
	"strconv"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/util/retry"

	"github.com/vencil/tenant-api/internal/federation/account"
)

// HighWaterAnnotation holds the largest AccountID ever reserved, as a
// decimal string. ⛔ helm/tenant-api's namespace data-move guard looks for it
// by this name.
const HighWaterAnnotation = "tenant-api.vibe/account-id-high-water"

// HighWaterBaseAnnotation holds the registry's next_account_id that the
// reservation in HighWaterAnnotation was planned from (account.Mark.Base).
// Both are written in one update.
const HighWaterBaseAnnotation = "tenant-api.vibe/account-id-high-water-base"

// HighWater is the mark on the store ConfigMap (account.HighWater).
type HighWater struct {
	client    kubernetes.Interface
	namespace string
	name      string
}

// NewHighWater returns the mark kept on the store ConfigMap namespace/name.
func NewHighWater(client kubernetes.Interface, namespace, name string) *HighWater {
	return &HighWater{client: client, namespace: namespace, name: name}
}

// Get returns the mark, or the zero Mark when the annotations are absent.
func (h *HighWater) Get(ctx context.Context) (account.Mark, error) {
	cctx, cancel := context.WithTimeout(ctx, k8sCallTimeout)
	defer cancel()
	cm, err := h.client.CoreV1().ConfigMaps(h.namespace).Get(cctx, h.name, metav1.GetOptions{})
	if err != nil {
		return account.Mark{}, fmt.Errorf("federation: get %s/%s: %w", h.namespace, h.name, err)
	}
	return h.parse(cm)
}

// Raise records m when m.Max is above the current Max and returns the mark
// now recorded. It never lowers the mark.
func (h *HighWater) Raise(ctx context.Context, m account.Mark) (account.Mark, error) {
	var result account.Mark
	err := retry.RetryOnConflict(retry.DefaultRetry, func() error {
		cctx, cancel := context.WithTimeout(ctx, k8sCallTimeout)
		defer cancel()
		cm, err := h.client.CoreV1().ConfigMaps(h.namespace).Get(cctx, h.name, metav1.GetOptions{})
		if err != nil {
			return fmt.Errorf("federation: get %s/%s: %w", h.namespace, h.name, err)
		}
		cur, err := h.parse(cm)
		if err != nil {
			return err
		}
		if m.Max <= cur.Max {
			result = cur
			return nil
		}
		if cm.Annotations == nil {
			cm.Annotations = map[string]string{}
		}
		cm.Annotations[HighWaterAnnotation] = strconv.FormatUint(uint64(m.Max), 10)
		cm.Annotations[HighWaterBaseAnnotation] = strconv.FormatUint(uint64(m.Base), 10)
		if _, err := h.client.CoreV1().ConfigMaps(h.namespace).Update(cctx, cm, metav1.UpdateOptions{}); err != nil {
			return err
		}
		result = m
		return nil
	})
	return result, err
}

// parse reads the annotations. A value that is not a uint32 fails closed:
// the mark is unknown, so nothing may be allocated against it.
func (h *HighWater) parse(cm *corev1.ConfigMap) (account.Mark, error) {
	var m account.Mark
	for key, dst := range map[string]*uint32{HighWaterAnnotation: &m.Max, HighWaterBaseAnnotation: &m.Base} {
		raw, ok := cm.Annotations[key]
		if !ok {
			continue
		}
		v, err := strconv.ParseUint(raw, 10, 32)
		if err != nil {
			return account.Mark{}, fmt.Errorf("federation: annotation %s on %s/%s holds %q, not an AccountID", key, h.namespace, h.name, raw)
		}
		*dst = uint32(v)
	}
	return m, nil
}
