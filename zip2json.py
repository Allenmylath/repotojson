import streamlit as st
import zipfile
import io
import json
import os

# --- Session State Initializations ---
if 'checkbox_states' not in st.session_state:
    st.session_state.checkbox_states = {}
if 'current_file_tree' not in st.session_state:
    st.session_state.current_file_tree = None
if 'last_uploaded_filename' not in st.session_state:
    st.session_state.last_uploaded_filename = None
if 'expanded_folders' not in st.session_state: # For custom expand/collapse
    st.session_state.expanded_folders = set()
if 'json_output_cache' not in st.session_state: # Initialize if not present
    st.session_state.json_output_cache = None


# --- Helper Functions ---

def build_file_tree_from_zip(zip_file_obj):
    tree = {}
    file_paths_in_zip = sorted([name for name in zip_file_obj.namelist() if not name.endswith('/')])

    for path_str in file_paths_in_zip:
        parts = path_str.split('/')
        current_level = tree
        for i, part in enumerate(parts):
            is_last_part = (i == len(parts) - 1)
            if is_last_part:
                current_level[part] = {'type': 'file', 'path': path_str}
            else:
                if part not in current_level:
                    current_level[part] = {
                        'type': 'folder',
                        'children': {},
                        'path': '/'.join(parts[:i+1]) + '/'
                    }
                elif current_level[part]['type'] == 'file':
                    st.warning(f"Path conflict: '{part}' was a file, now treated as a folder.")
                    current_level[part]['type'] = 'folder'
                    if 'children' not in current_level[part]:
                         current_level[part]['children'] = {}
                    current_level[part]['path'] = '/'.join(parts[:i+1]) + '/'
                current_level = current_level[part]['children']
    return tree

def render_tree_ui(data_dict, indent_level=0):
    sorted_items = sorted(data_dict.items(), key=lambda x: (x[1]['type'] == 'file', x[0]))

    for name, item in sorted_items:
        current_item_path = item['path']
        is_folder = item['type'] == 'folder'
        has_children = is_folder and item.get('children')
        item_label_prefix = "📁" if is_folder else "📄"
        base_display_label_text = f"{chr(160) * 4 * indent_level}{item_label_prefix} {name}"

        if has_children:
            col1, col2 = st.columns([0.92, 0.08]) # Main label and button
            with col1:
                is_selected_for_json = st.session_state.checkbox_states.get(current_item_path, False)
                new_selection_state = st.checkbox(base_display_label_text,
                                                  key=f"select_{current_item_path}",
                                                  value=is_selected_for_json)
                if new_selection_state != is_selected_for_json:
                    st.session_state.checkbox_states[current_item_path] = new_selection_state
            
            with col2:
                is_currently_expanded = current_item_path in st.session_state.expanded_folders
                toggle_symbol = "➖" if is_currently_expanded else "➕"
                if st.button(toggle_symbol, key=f"toggle_{current_item_path}", help=f"Expand/Collapse {name}"):
                    if is_currently_expanded:
                        st.session_state.expanded_folders.remove(current_item_path)
                    else:
                        st.session_state.expanded_folders.add(current_item_path)
                    st.rerun()
        else:
            is_selected_for_json = st.session_state.checkbox_states.get(current_item_path, False)
            new_selection_state = st.checkbox(base_display_label_text,
                                              key=f"select_{current_item_path}",
                                              value=is_selected_for_json)
            if new_selection_state != is_selected_for_json:
                st.session_state.checkbox_states[current_item_path] = new_selection_state

        if has_children and (current_item_path in st.session_state.expanded_folders):
            render_tree_ui(item['children'], indent_level + 1)

def get_node_details_from_tree(path_key, tree_root):
    parts = path_key.strip('/').split('/') 
    current_node_dict = tree_root
    node_info = None
    for i, p_part in enumerate(parts):
        if p_part in current_node_dict:
            node_info = current_node_dict[p_part]
            if node_info['type'] == 'folder' and i < len(parts) - 1 :
                current_node_dict = node_info.get('children', {})
            elif i == len(parts) -1: 
                return node_info
            else: return None
        else: return None
    return node_info 

def collect_final_selected_files(zip_file_obj_for_processing, initial_file_tree):
    user_selected_paths_from_ui = {path for path, checked in st.session_state.checkbox_states.items() if checked}
    final_files_for_json = set()
    all_actual_files_in_zip = [name for name in zip_file_obj_for_processing.namelist() if not name.endswith('/')]

    for selected_path_key in user_selected_paths_from_ui:
        node_details = get_node_details_from_tree(selected_path_key, initial_file_tree)
        if node_details:
            if node_details['type'] == 'file':
                final_files_for_json.add(node_details['path'])
            elif node_details['type'] == 'folder':
                folder_prefix_path = node_details['path'] 
                for file_in_zip in all_actual_files_in_zip:
                    if file_in_zip.startswith(folder_prefix_path):
                        final_files_for_json.add(file_in_zip)
    return sorted(list(final_files_for_json))

def build_nested_json_from_paths(selected_file_paths, zip_file_obj):
    output_json_structure = {}
    if not selected_file_paths: return output_json_structure

    common_prefix = ""
    if len(selected_file_paths) > 0:
        common_prefix = os.path.commonpath(selected_file_paths)
        if common_prefix:
            is_prefix_actually_file = common_prefix in selected_file_paths and not common_prefix.endswith('/')
            if is_prefix_actually_file or not all(f.startswith(common_prefix + ('/' if not common_prefix.endswith('/') else '')) for f in selected_file_paths if f != common_prefix):
                common_prefix = os.path.dirname(common_prefix)
            if common_prefix and not common_prefix.endswith('/'):
                 common_prefix += '/'
        if common_prefix == "./" or common_prefix == ".": common_prefix = ""

    for file_path_in_zip in selected_file_paths:
        try:
            file_content_bytes = zip_file_obj.read(file_path_in_zip)
            file_content_str = file_content_bytes.decode('utf-8', errors='replace')
        except Exception as e:
            file_content_str = f"Error reading/decoding file '{file_path_in_zip}': {e}."

        relative_file_path = file_path_in_zip
        if common_prefix and file_path_in_zip.startswith(common_prefix):
            relative_file_path = file_path_in_zip[len(common_prefix):]
        
        path_segments = relative_file_path.split('/')
        current_dict_level = output_json_structure
        for i, segment in enumerate(path_segments):
            if not segment: continue
            is_last_segment = (i == len(path_segments) - 1)
            if is_last_segment:
                current_dict_level[segment] = file_content_str
            else:
                if segment not in current_dict_level or not isinstance(current_dict_level[segment], dict):
                    if segment in current_dict_level:
                         st.warning(f"JSON structure conflict: '{segment}' was a file, now treated as a folder.")
                    current_dict_level[segment] = {}
                current_dict_level = current_dict_level[segment]
    return output_json_structure

# --- Streamlit App UI ---
st.set_page_config(layout="wide")
st.title("ZIP Repo to JSON Converter 🧥➡️📄")
st.markdown("""
Upload a `.zip` file (e.g., from a GitHub repository). 
Visually select the files and folders to include. 
Click "Convert to JSON" to generate a single JSON file 
with the selected contents, preserving the folder structure. 
Folders with a ➕ can be expanded to show their contents, and ➖ to collapse.
""")

uploaded_zip_file = st.file_uploader("📤 Upload your ZIP file here", type="zip")

if uploaded_zip_file is not None:
    if st.session_state.last_uploaded_filename != uploaded_zip_file.name:
        st.session_state.checkbox_states = {}
        st.session_state.current_file_tree = None
        st.session_state.last_uploaded_filename = uploaded_zip_file.name
        st.session_state.expanded_folders = set() # Reset expanded folders
        st.session_state.json_output_cache = None # Reset json output

    try:
        zip_file_bytes = io.BytesIO(uploaded_zip_file.getvalue())
        with zipfile.ZipFile(zip_file_bytes, 'r') as zf:
            if st.session_state.current_file_tree is None:
                st.session_state.current_file_tree = build_file_tree_from_zip(zf)

            st.subheader("🌳 Select Files and Folders:")
            if st.session_state.current_file_tree:
                render_tree_ui(st.session_state.current_file_tree, indent_level=0)
            else:
                st.warning("Could not parse the file tree from the ZIP. It might be empty or structured unusually.")

            if st.button("✨ Convert to JSON", type="primary"):
                zip_file_bytes.seek(0) 
                with zipfile.ZipFile(zip_file_bytes, 'r') as zf_process:
                    selected_files_for_json = collect_final_selected_files(zf_process, st.session_state.current_file_tree)
                    if not selected_files_for_json:
                        st.warning("No files or folders selected. Please make a selection.")
                        st.session_state.json_output_cache = None
                    else:
                        st.session_state.json_output_cache = build_nested_json_from_paths(selected_files_for_json, zf_process)
            
            if st.session_state.json_output_cache is not None:
                json_string_output = json.dumps(st.session_state.json_output_cache, indent=2)
                st.subheader("📄 Generated JSON Output:")
                st.json(json_string_output) 
                st.download_button(
                    label="💾 Download JSON File",
                    data=json_string_output,
                    file_name=f"{st.session_state.last_uploaded_filename.replace('.zip', '')}_selected.json",
                    mime="application/json"
                )

    except zipfile.BadZipFile:
        st.error("❌ Error: The uploaded file is not a valid ZIP archive or it might be corrupted.")
        st.session_state.current_file_tree = None
        st.session_state.checkbox_states = {}
        st.session_state.expanded_folders = set()
    except Exception as e:
        st.error(f"An unexpected error occurred: {e}")
        # import traceback
        # st.code(traceback.format_exc()) 
else:
    st.info("☝️ Waiting for a ZIP file to be uploaded.")
    if st.session_state.last_uploaded_filename is not None: 
        st.session_state.checkbox_states = {}
        st.session_state.current_file_tree = None
        st.session_state.last_uploaded_filename = None
        st.session_state.expanded_folders = set()
        st.session_state.json_output_cache = None