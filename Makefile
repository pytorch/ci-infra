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
			echo "==== TFLINT: aws/$$account/$$region ============================================" ; \
			$(MAKE) tflint || exit 1 ; \
			popd ; \
		done ; \
		popd ; \
	done
