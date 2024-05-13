SHELL=/bin/bash -o pipefail

.PHONY: clean
clean:
	$(RM) -r .terraform
	$(RM) -r modules/arc/.terraform
	$(RM) -r modules/arc/.terraform.lock.hcl
	$(RM) -r tf-modules
	$(RM) -r venv

.PHONY: tflint
tflint:
	cd modules/arc && terraform init && tflint --init && tflint --module --color --minimum-failure-severity=warning --recursive
	cd aws ; for account in ./*/ ; do \
		pushd $$account ; \
		for region in ./*/ ; do \
			pushd $$region ; \
			echo "==== START TFLINT: aws/$$account/$$region ============================================" ; \
			$(MAKE) tflint || exit 1 ; \
			echo "==== END TFLINT: aws/$$account/$$region ============================================" ; \
			popd ; \
		done ; \
		popd ; \
	done

.PHONY: plan
plan:
	cd aws ; for account in ./*/ ; do \
		pushd $$account ; \
		for region in ./*/ ; do \
			pushd $$region ; \
			echo "==== START make plan: aws/$$account/$$region ============================================" ; \
			$(MAKE) plan || exit 1 ; \
			echo "==== END make plan: aws/$$account/$$region ============================================" ; \
			popd ; \
		done ; \
		popd ; \
	done

.PHONY: apply
apply:
	cd aws ; for account in ./*/ ; do \
		pushd $$account ; \
		for region in ./*/ ; do \
			pushd $$region ; \
			echo "==== START make apply: aws/$$account/$$region ============================================" ; \
			$(MAKE) apply || exit 1 ; \
			echo "==== END make apply: aws/$$account/$$region ============================================" ; \
			popd ; \
		done ; \
		popd ; \
	done

.PHONY: apply-arc-canary
apply-arc-canary:
	cd aws ; for account in ./*/ ; do \
		pushd $$account ; \
		for region in ./*/ ; do \
			pushd $$region ; \
			echo "==== START make apply-arc-canary: aws/$$account/$$region ============================================" ; \
			$(MAKE) apply-arc-canary || exit 1 ; \
			echo "==== END make apply-arc-canary: aws/$$account/$$region ============================================" ; \
			popd ; \
		done ; \
		popd ; \
	done

.PHONY: apply-arc-canary-monitoring
apply-arc-canary-monitoring:
	cd aws ; for account in ./*/ ; do \
		pushd $$account ; \
		for region in ./*/ ; do \
			pushd $$region ; \
			echo "==== START make apply-arc-canary-monitoring: aws/$$account/$$region ============================================" ; \
			$(MAKE) apply-arc-canary-monitoring || exit 1 ; \
			echo "==== END make apply-arc-canary-monitoring: aws/$$account/$$region ============================================" ; \
			popd ; \
		done ; \
		popd ; \
	done

.PHONY: arc-canary-monitoring
arc-canary-monitoring:
	cd aws ; for account in ./*/ ; do \
		pushd $$account ; \
		for region in ./*/ ; do \
			pushd $$region ; \
			echo "==== START make arc-canary-monitoring: aws/$$account/$$region ============================================" ; \
			$(MAKE) arc-canary-monitoring || exit 1 ; \
			echo "==== END make arc-canary-monitoring: aws/$$account/$$region ============================================" ; \
			popd ; \
		done ; \
		popd ; \
	done

.PHONY: apply-arc-vanguard
apply-arc-vanguard:
	cd aws ; for account in ./*/ ; do \
		pushd $$account ; \
		for region in ./*/ ; do \
			pushd $$region ; \
			echo "==== START make apply-arc-vanguard: aws/$$account/$$region ============================================" ; \
			$(MAKE) apply-arc-vanguard || exit 1 ; \
			echo "==== END make apply-arc-vanguard: aws/$$account/$$region ============================================" ; \
			popd ; \
		done ; \
		popd ; \
	done

.PHONY: apply-arc-prod
apply-arc-prod:
	cd aws ; for account in ./*/ ; do \
		pushd $$account ; \
		for region in ./*/ ; do \
			pushd $$region ; \
			echo "==== START make apply-arc-prod: aws/$$account/$$region ============================================" ; \
			$(MAKE) apply-arc-prod || exit 1 ; \
			echo "==== END make apply-arc-prod: aws/$$account/$$region ============================================" ; \
			popd ; \
		done ; \
		popd ; \
	done

.PHONY: arc-canary
arc-canary:
	cd aws ; for account in ./*/ ; do \
		pushd $$account ; \
		for region in ./*/ ; do \
			pushd $$region ; \
			echo "==== START make arc-canary: aws/$$account/$$region ============================================" ; \
			$(MAKE) arc-canary || exit 1 ; \
			echo "==== END make arc-canary: aws/$$account/$$region ============================================" ; \
			popd ; \
		done ; \
		popd ; \
	done

.PHONY: arc-vanguard
arc-vanguard:
	cd aws ; for account in ./*/ ; do \
		pushd $$account ; \
		for region in ./*/ ; do \
			pushd $$region ; \
			echo "==== START make arc-vanguard: aws/$$account/$$region ============================================" ; \
			$(MAKE) arc-vanguard || exit 1 ; \
			echo "==== END make arc-vanguard: aws/$$account/$$region ============================================" ; \
			popd ; \
		done ; \
		popd ; \
	done

.PHONY: arc-vanguard-off
arc-vanguard-off:
	cd aws ; for account in ./*/ ; do \
		pushd $$account ; \
		for region in ./*/ ; do \
			pushd $$region ; \
			echo "==== START make arc-vanguard-off: aws/$$account/$$region ============================================" ; \
			$(MAKE) arc-vanguard-off || exit 1 ; \
			echo "==== END make arc-vanguard-off: aws/$$account/$$region ============================================" ; \
			popd ; \
		done ; \
		popd ; \
	done

.PHONY: arc-prod
arc-prod:
	cd aws ; for account in ./*/ ; do \
		pushd $$account ; \
		for region in ./*/ ; do \
			pushd $$region ; \
			echo "==== START make arc-prod: aws/$$account/$$region ============================================" ; \
			$(MAKE) arc-prod || exit 1 ; \
			echo "==== END make arc-prod: aws/$$account/$$region ============================================" ; \
			popd ; \
		done ; \
		popd ; \
	done

.PHONY: k8s-delete-stuck-resources
k8s-delete-stuck-resources:
	cd aws ; for account in ./*/ ; do \
		pushd $$account ; \
		for region in ./*/ ; do \
			pushd $$region ; \
			echo "==== START make k8s-delete-stuck-resources: aws/$$account/$$region ============================================" ; \
			$(MAKE) k8s-delete-stuck-resources || exit 1 ; \
			echo "==== END make k8s-delete-stuck-resources: aws/$$account/$$region ============================================" ; \
			popd ; \
		done ; \
		popd ; \
	done

venv/bin/pip:
	virtualenv venv
	venv/bin/pip install -r requirements.txt

tf-modules/VERSIONS: venv/bin/pip Terrafile
	venv/bin/python scripts/terrafile_lambdas.py -t Terrafile -m tf-modules

.PHONY: terrafile
terrafile: tf-modules/VERSIONS

# Deployment
.PHONY: open-rel-pr
open-rel-pr: venv/bin/pip
	venv/bin/python ./scripts/deployment.py --debug open-rel-pr

.PHONY: wait-check-deployment
wait-check-deployment: venv/bin/pip
	[ "$(RELEASE_ACTION_NAME)" != "" ] || (echo "RELEASE_ACTION_NAME not set"; exit 1)
	venv/bin/python ./scripts/deployment.py --debug wait-check-deployment --release-action-name "$(RELEASE_ACTION_NAME)" --comment-to-add "$(COMMENT_TO_ADD)" --ignore-if-label "$(IGNORE_IF_LABEL)"

.PHONY: wait-check-user-comment
wait-check-user-comment: venv/bin/pip
	[ "$(WAIT_COMMENT)" != "" ] || (echo "WAIT_COMMENT not set"; exit 1)
	venv/bin/python ./scripts/deployment.py --debug wait-check-user-comment --comment "$(WAIT_COMMENT)"

.PHONY: wait-check-bot-comment
wait-check-bot-comment: venv/bin/pip
	[ "$(WAIT_COMMENT)" != "" ] || (echo "WAIT_COMMENT not set"; exit 1)
	venv/bin/python ./scripts/deployment.py --debug wait-check-bot-comment --comment "$(WAIT_COMMENT)"

.PHONY: react-pr-comment
react-pr-comment: venv/bin/pip
	[ "$(COMMENTS)" != "" ] || (echo "COMMENTS not set"; exit 1)
	[ "$(LABELS)" != "" ] || (echo "LABELS not set"; exit 1)
	[ "$(CHECK_REMOVE_LABELS)" != "" ] || (echo "CHECK_REMOVE_LABELS not set"; exit 1)
	[ "$(CHECK_COMMENTS)" != "" ] || (echo "CHECK_COMMENTS not set"; exit 1)
	venv/bin/python ./scripts/deployment.py --debug react-pr-comment --comments "$(COMMENTS)" --labels "$(LABELS)" --check-remove-labels "$(CHECK_REMOVE_LABELS)" --check-comments "$(CHECK_COMMENTS)"

.PHONY: add-comment-to-pr
add-comment-to-pr: venv/bin/pip
	[ "$(COMMENT_TO_ADD)" != "" ] || (echo "COMMENT_TO_ADD not set"; exit 1)
	venv/bin/python ./scripts/deployment.py --debug add-comment-to-pr --comment "$(COMMENT_TO_ADD)"

.PHONY: wait-check-pr-approved
wait-check-pr-approved: venv/bin/pip
	venv/bin/python ./scripts/deployment.py --debug wait-check-pr-approved

.PHONY: close-pr
close-pr: venv/bin/pip
	venv/bin/python ./scripts/deployment.py --debug close-pr

.PHONY: merge-pr
merge-pr: venv/bin/pip
	venv/bin/python ./scripts/deployment.py --debug merge-pr

.PHONY: build-runner-images
build-runner-images: venv/bin/pip
	cd docker/arc-runner && $(MAKE) all
	cd docker/arc-dind && $(MAKE) all
