"""Wait for the one corpus encoder, then execute the already authorized fixed workflow."""
from common_r1 import *
import argparse,time

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--encoder-pid',type=int,required=True);args=parser.parse_args()
    while not (REVIEW/'INDEX_HASHES.json').exists():
        try:os.kill(args.encoder_pid,0)
        except ProcessLookupError:raise RuntimeError('Encoder exited before a complete hash manifest; no evaluation/generation')
        time.sleep(30)
    from finish import main as finish
    finish()
if __name__=='__main__':main()
