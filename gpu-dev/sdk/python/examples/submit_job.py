"""Submit a training job to a GPU server and wait for results.

Usage:
    python submit_job.py
"""
from gpu_dev import GpuDev

client = GpuDev()

# Reserve a T4 GPU, auto-cancel when done
with client.reserve(gpu_type="t4", hours=1, name="training-job", on_progress=True) as sb:
    print(f"\nReserved: {sb.id[:8]} on {sb.instance_type}")
    print(f"SSH: {sb.ssh_command}\n")

    # Upload training script
    sb.upload("./train.py", "/home/dev/train.py")

    # Run training
    print("Starting training...")
    result = sb.exec("cd /home/dev && python train.py 2>&1", timeout=600)
    print(result.stdout)

    if result.exit_code != 0:
        print(f"Training failed (exit {result.exit_code})")
        print(result.stderr)
    else:
        # Download results
        sb.download("/home/dev/output/", "./results/")
        print("Results downloaded to ./results/")

    # Check logs if something went wrong
    if result.exit_code != 0:
        print("\nReservation logs:")
        for entry in sb.logs("error"):
            print(f"  [{entry['timestamp'][11:23]}] {entry['message']}")

# Reservation auto-cancelled
print("Done — reservation cleaned up")
