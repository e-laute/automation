#!/usr/bin/env python3
"""
Upload PROV-O TTL data directly to GraphDB repository.

Usage:
    python upload_to_graphdb.py <mei_file_or_directory>

This script extracts MEI metadata, converts it to PROV-O TTL format,
and uploads it directly to a GraphDB repository without saving files locally.
"""
import os
import sys
import subprocess
import requests
from pathlib import Path
class GraphDBUploader:
    def __init__(self):
        self.base_url = os.environ['GRAPHDB_URL']
        self.repository = os.environ['GRAPHDB_REPOSITORY']
        self.username = os.environ['GRAPHDB_USERNAME']
        self.password = os.environ['GRAPHDB_PASSWORD']
        self.timeout = 30
        self.max_retries = 3
        
        if not all([self.base_url, self.repository, self.username, self.password]):
            raise ValueError("Missing required GraphDB configuration in graphdb.env file")
        
        self.session = requests.Session()
        self.session.auth = (self.username, self.password)
        self.session.headers.update({
            'Content-Type': 'application/x-turtle',
            'Accept': 'application/json'
        })
    
    def test_connection(self):
        """Test connection to GraphDB repository."""
        try:
            # Test repository access using the size endpoint
            url = f"{self.base_url}/repositories/{self.repository}/size"
            response = self.session.get(url, timeout=self.timeout)
            
            if response.status_code == 200:
                print(f"✅ Successfully connected to GraphDB repository: {self.repository}")
                return True
            else:
                print(f"❌ Connection failed with status code: {response.status_code}")
                print(f"   Response headers: {response.headers}")
                print(f"   Response content: {response.text}")
                if response.status_code == 401:
                    print(f"   Authentication failed. Please check username/password in graphdb.env")
                elif response.status_code == 404:
                    print(f"   Repository '{self.repository}' not found. Please check repository name.")
                return False
                
        except requests.exceptions.RequestException as e:
            print(f"❌ Connection error: {e}")
            return False
    
    def upload_ttl_data(self, ttl_data, graph_name=None):
        """Upload TTL data to GraphDB repository."""
        if not ttl_data.strip():
            print("⚠️  No TTL data to upload")
            return False
        
        # GraphDB REST API endpoint for statements
        url = f"{self.base_url}/repositories/{self.repository}/statements"
        
        # Add graph parameter if specified
        if graph_name:
            url += f"?context={graph_name}"
        
        try:
            print(f"📤 Uploading TTL data to GraphDB...")
            print(f"   URL: {url}")
            print(f"   Data size: {len(ttl_data)} characters")
            
            response = self.session.post(
                url,
                data=ttl_data.encode('utf-8'),
                timeout=self.timeout
            )
            
            if response.status_code == 204:  # No content, success
                print(f"✅ Successfully uploaded TTL data to GraphDB")
                return True
            else:
                print(f"❌ Upload failed with status code: {response.status_code}")
                print(f"   Response: {response.text}")
                return False
                
        except requests.exceptions.RequestException as e:
            print(f"❌ Upload error: {e}")
            return False
    
    def get_repository_stats(self):
        """Get repository statistics."""
        try:
            url = f"{self.base_url}/repositories/{self.repository}/size"
            response = self.session.get(url, timeout=self.timeout)
            
            if response.status_code == 200:
                stats = response.json()
                return stats
            else:
                print(f"⚠️  Could not retrieve repository stats: {response.status_code}")
                return None
                
        except requests.exceptions.RequestException as e:
            print(f"⚠️  Error retrieving stats: {e}")
            return None

def find_mei_files(directory):
    """Find all .mei and .tei files in the directory structure."""
    mei_files = []
    directories_searched = set()
    
    print(f"🔍 Searching for MEI/TEI files in: {directory}")
    print("   Scanning subdirectories recursively...")
    
    for root, dirs, files in os.walk(directory):
        directories_searched.add(root)
        
        # Check if any MEI files are in this directory
        mei_in_dir = [f for f in files if f.endswith('.mei') or f.endswith('.tei')]
        if mei_in_dir:
            print(f"   📁 Found {len(mei_in_dir)} MEI/TEI files in: {root}")
            for file in mei_in_dir:
                full_path = os.path.join(root, file)
                mei_files.append(full_path)
    
    print(f"   ✅ Searched {len(directories_searched)} directories total")
    return mei_files

def extract_prov_from_file(file_path):
    """Extract PROV-O RDF from a single MEI file."""
    try:
        # determine the directory our script is running in, and launch anohter script 'parse_provenance_prov.py' within the same directory as a subprocess
        script_path = Path(__file__).resolve().parent /  'parse_provenance_prov.py'

        result = subprocess.run(
            [sys.executable, str(script_path), file_path],
            capture_output=True,
            text=True,
            check=True
        )
        return {
            'file_path': file_path,
            'success': True,
            'ttl_output': result.stdout.strip()
        }
    except subprocess.CalledProcessError as e:
        return {
            'file_path': file_path,
            'success': False,
            'error': f"Script error: {e.stderr.strip()}"
        }
    except Exception as e:
        return {
            'file_path': file_path,
            'success': False,
            'error': f"Unexpected error: {e}"
        }

def main():
    input_path = Path("./caller-repo/")
    
    # Initialize GraphDB uploader
    try:
        uploader = GraphDBUploader()
    except ValueError as e:
        print(f"❌ Configuration error: {e}")
        print("Please check your graphdb.env file and ensure all required values are set.")
        sys.exit(1)
    
    # Test connection
    print("🔗 Testing GraphDB connection...")
    if not uploader.test_connection():
        sys.exit(1)
    
    # Get initial repository stats
    print("\n📊 Getting repository statistics...")
    initial_stats = uploader.get_repository_stats()
    if initial_stats is not None:
        print(f"   Initial statements: {initial_stats}")
    
    # Process files
    if input_path.is_file():
        # Single file
        print(f"\n📁 Processing single file: {input_path}")
        results = [extract_prov_from_file(str(input_path))]
    else:
        # Directory
        print(f"\n📁 Processing directory: {input_path}")
        mei_files = find_mei_files(str(input_path))
        if not mei_files:
            print("❌ No .mei or .tei files found in the specified directory.")
            sys.exit(1)
        
        print(f"\n📋 Summary: Found {len(mei_files)} MEI/TEI files to process")
        print("Files to be processed:")
        for file in mei_files:
            print(f"  - {file}")
        print()
        results = []
        for i, mei_file in enumerate(mei_files, 1):
            print(f"  [{i}/{len(mei_files)}] Processing: {mei_file}")
            result = extract_prov_from_file(mei_file)
            results.append(result)
            
            # Show progress
            if result['success']:
                print(f"     ✅ Extracted PROV-O data successfully")
            else:
                print(f"     ❌ Failed to extract: {result['error']}")
    
    # Upload results
    successful_uploads = 0
    failed_uploads = 0
    total_statements = 0
    
    print(f"\n🚀 Starting upload to GraphDB...")
    print("="*60)
    
    for i, result in enumerate(results, 1):
        if result['success']:
            print(f"\n[{i}/{len(results)}] Uploading: {result['file_path']}")
            # Upload to GraphDB without named graph context for now
            if uploader.upload_ttl_data(result['ttl_output']):
                successful_uploads += 1
                print(f"   ✅ Uploaded successfully (default graph)")
                
                # Count statements (rough estimate)
                statement_count = result['ttl_output'].count('.')
                total_statements += statement_count
                print(f"   📊 Statements: ~{statement_count}")
            else:
                failed_uploads += 1
                print(f"   ❌ Upload failed")
        else:
            failed_uploads += 1
            print(f"\n[{i}/{len(results)}] ❌ Skipped: {result['file_path']} - {result['error']}")
    
    # Get final repository stats
    print(f"\n📊 Getting final repository statistics...")
    final_stats = uploader.get_repository_stats()
    
    # Summary
    print("\n" + "="*60)
    print("UPLOAD SUMMARY")
    print("="*60)
    print(f"Total files processed: {len(results)}")
    print(f"Successful uploads: {successful_uploads}")
    print(f"Failed uploads: {failed_uploads}")
    print(f"Total statements uploaded: ~{total_statements}")
    
    if initial_stats is not None and final_stats is not None:
        initial_stmts = initial_stats
        final_stmts = final_stats
        added_stmts = final_stmts - initial_stmts
        print(f"Repository statements before: {initial_stmts}")
        print(f"Repository statements after: {final_stmts}")
        print(f"Statements added: {added_stmts}")
    
    print(f"\n🔗 GraphDB Repository: {uploader.base_url}/webview")
    print(f"📊 Repository: {uploader.repository}")
    
    if failed_uploads > 0:
        print(f"\n⚠️  {failed_uploads} upload(s) failed. Check the logs above for details.")
        sys.exit(1)
    else:
        print(f"\n✅ All uploads completed successfully!")
        sys.exit(0)

if __name__ == "__main__":
    main() 
