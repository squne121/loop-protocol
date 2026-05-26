import os
import yaml
import sys
import glob

def validate_file(file_path):
    print(f"Validating {file_path}...")
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
            if not content.startswith('---'):
                print(f"  [SKIP] No frontmatter found.")
                return True
            
            # Find the end of frontmatter
            parts = content.split('---', 2)
            if len(parts) < 3:
                print(f"  [ERROR] Incomplete frontmatter (missing closing '---').")
                return False
            
            frontmatter = parts[1]
            yaml.safe_load(frontmatter)
            print(f"  [OK] Valid frontmatter.")
            return True
    except yaml.YAMLError as exc:
        print(f"  [ERROR] YAML parsing failed: {exc}")
        return False
    except Exception as exc:
        print(f"  [ERROR] Unexpected error: {exc}")
        return False

def main():
    base_dir = os.getcwd()
    patterns = [
        '.claude/agents/*.md',
        '.claude/skills/*/SKILL.md'
    ]
    
    all_valid = True
    for pattern in patterns:
        files = glob.glob(os.path.join(base_dir, pattern))
        for file_path in files:
            if not validate_file(file_path):
                all_valid = False
    
    if not all_valid:
        print("\nFrontmatter validation FAILED.")
        sys.exit(1)
    else:
        print("\nFrontmatter validation PASSED.")
        sys.exit(0)

if __name__ == "__main__":
    main()
