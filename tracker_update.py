import requests

url = "https://raw.githubusercontent.com/ngosang/trackerslist/refs/heads/master/trackers_best.txt"
response = requests.get(url)
response.raise_for_status()

lines = response.text.split()
hosts = []

for line in lines:
    line = line.strip()
    if line.startswith("udp://"):
        line = line[len("udp://") :]
    if line.endswith("/announce"):
        line = line[: -len("/announce")]
    hosts.append(line)

# Deduplicate and sort
hosts = sorted(set(hosts))

for host in hosts:
    print(host)
