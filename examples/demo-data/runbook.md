# ErrImagePull Runbook

## Symptoms
- Pods stuck in ErrImagePull or ImagePullBackOff
- Deployment shows 0/N ready replicas

## Steps
1. Check the image reference: `kubectl describe pod <pod> | grep Image`
2. Verify the image exists in the registry: `skopeo inspect docker://<image>`
3. Check registry credentials: `kubectl get secret <pull-secret> -o yaml`
4. Fix the image reference in the deployment
5. Rollout: `kubectl rollout restart deployment/<name>`

## Escalation
If the image doesn't exist in any registry, contact the CI/CD team.
